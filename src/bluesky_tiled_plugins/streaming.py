import functools
import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol, cast

from pandas import DataFrame
from tiled.client.base import BaseClient
from tiled.client.container import Container
from tiled.client.stream import (
    ArraySubscription,
    ContainerSubscription,
    LiveArrayData,
    LiveArrayRef,
    LiveChildCreated,
    LiveTableData,
    Subscription,
    TableSubscription,
)

logger = logging.getLogger(__name__)


_EMPTY_START_DOCUMENT: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True)
class BlueskyStreamUpdate:
    """
    A selected Tiled update from a Bluesky data stream.

    Parameters
    ----------
    run_uid : str
        Key of the direct ``BlueskyRun`` parent.
    stream_name : str
        Name of the Bluesky data stream containing the updated node.
    data_keys : tuple of str
        Requested data keys represented by this update. A table update
        may contain several selected scalar keys; an array update contains one.
    update : LiveArrayData, LiveArrayRef, or LiveTableData
        Original Tiled live update. It is retained without decoding until
        :meth:`data` is called.
    sequence : int
        Native positive per-node Tiled streaming sequence number for ``update``.
        Use it to correlate updates and detect duplicates or gaps for one node.
    """

    run_uid: str
    stream_name: str
    data_keys: tuple[str, ...]
    update: LiveArrayData | LiveArrayRef | LiveTableData
    sequence: int = 0

    def data(self) -> Any:
        """
        Decode the original Tiled live update.

        Returns
        -------
        Any
            Tiled's decoded array or table representation. When Tiled's standard
            table decoder returns a pandas DataFrame, only :attr:`data_keys` are
            included. Other Tiled-decoded table representations are returned
            unchanged.

        Notes
        -----
        Decoding happens only when this method is called. Array references retain
        Tiled's normal fetch behavior.
        """
        data = self.update.data()
        if isinstance(self.update, LiveTableData) and isinstance(data, DataFrame):
            return data.loc[:, list(self.data_keys)]
        return data


class _Subscribable(Protocol):
    @property
    def uri(self) -> str: ...

    def subscribe(self) -> Subscription: ...


class BlueskyStreamSubscription:
    """Own recursive Tiled subscriptions for selected Bluesky event streams."""

    def __init__(
        self,
        container: Container,
        streams: Mapping[str, tuple[str, ...] | None] | None,
        callback: Callable[[BlueskyStreamUpdate], None],
        *,
        start: int | None,
        max_size: int,
        run_filter: Callable[[LiveChildCreated], bool] | None,
    ) -> None:
        """
        Create and start a managed stream subscription.

        Use :func:`subscribe_to_streams` for the public input boundary.

        Parameters
        ----------
        container : tiled.client.container.Container
            Direct parent of the ``BlueskyRun`` nodes created by the associated
            :class:`~bluesky_tiled_plugins.TiledWriter`.
        streams : Mapping[str, tuple of str or None] or None
            Mapping from Bluesky event-stream name to its data-key selection.
            A mapping value of ``None`` selects every streamable array and table key
            in that stream. An outer ``None`` selects every event stream and all its
            streamable array and table keys. Ragged and bytes nodes are ignored.
        callback : Callable[[BlueskyStreamUpdate], None]
            Function called with each selected Tiled live update. Tiled
            executes callbacks asynchronously; exceptions raised by this function
            are not caught by this manager.
        start : int or None
            Tiled sequence number supplied to every owned subscription. The default,
            ``0``, replays records retained by the streaming cache. ``None`` receives
            only new records.
        max_size : int
            Maximum incoming WebSocket message size in bytes. Defaults to
            ``1_000_000``.
        run_filter : Callable[[LiveChildCreated], bool] or None
            Optional raw Tiled run predicate.

        Raises
        ------
        Exception
            Any error raised while Tiled establishes the root subscription. Any
            partially established owned subscriptions are disconnected first.

        Notes
        -----
        This constructor starts and connects the root container subscription
        before returning. Future run descendants cannot exist until their writer
        creates them. Producer-closed descendant streams are released after their
        native callbacks drain.
        """
        self._streams = None if streams is None else dict(streams)
        self._callback = callback
        self._run_filter = run_filter
        self._start = start
        self._max_size = max_size
        self._lock = threading.Lock()
        self._closed = False
        self._subscriptions: dict[
            str,
            tuple[Subscription, Callable[..., None], Callable[[Subscription], None]],
        ] = {}
        # URI keys in global parent-before-child creation order. Reversing this
        # linear extension of the hierarchy tears down leaves before parents.
        self._ordered_subscription_uris: list[str] = []

        established = False
        try:
            self._subscribe_container(container, self._handle_container_child)
            established = True
        finally:
            if not established:
                try:
                    self.disconnect()
                except Exception:
                    logger.exception("Failed to disconnect root Tiled subscription")

    @property
    def closed(self) -> bool:
        """
        Whether teardown has begun.

        Returns
        -------
        bool
            ``True`` after :meth:`disconnect` marks this manager closed. Tiled
            subscriptions may still be completing their blocking teardown.
        """
        with self._lock:
            return self._closed

    def disconnect(self) -> None:
        """
        Disconnect every owned Tiled subscription.

        Owned data-node subscriptions are disconnected before stream, run, and
        container subscriptions. This method is idempotent and blocks while Tiled
        closes sockets and waits for its subscription threads.

        Raises
        ------
        Exception
            The first error raised while disconnecting an owned Tiled
            subscription, after teardown is attempted for every owned
            subscription.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            subscriptions = tuple(
                self._subscriptions[uri][0]
                for uri in reversed(self._ordered_subscription_uris)
            )
            self._subscriptions.clear()
            self._ordered_subscription_uris.clear()

        first_error: Exception | None = None
        for subscription in subscriptions:
            try:
                subscription.disconnect()
            except Exception as error:
                if first_error is None:
                    first_error = error
                logger.exception(
                    "Failed to disconnect Tiled subscription %s", subscription
                )
        if first_error is not None:
            raise first_error

    def __enter__(self) -> "BlueskyStreamSubscription":
        return self

    def __exit__(self, *args: object) -> None:
        self.disconnect()

    def _subscribe_container(
        self,
        node: Container,
        callback: Callable[[LiveChildCreated], None],
    ) -> None:
        def attach(subscription: Subscription) -> None:
            cast(ContainerSubscription, subscription).child_created.add_callback(
                callback
            )

        self._subscribe(node, callback, attach)

    def _subscribe_data(
        self,
        node: _Subscribable,
        run_uid: str,
        stream_name: str,
        data_keys: tuple[str, ...],
    ) -> None:
        callback = functools.partial(
            self._handle_data_update, run_uid, stream_name, data_keys
        )

        def attach(subscription: Subscription) -> None:
            cast(
                ArraySubscription | TableSubscription, subscription
            ).new_data.add_callback(callback)

        self._subscribe(node, callback, attach)

    def _subscribe(
        self,
        node: _Subscribable,
        callback: Callable[..., None],
        attach: Callable[[Subscription], None],
    ) -> None:
        uri = node.uri
        subscription: Subscription | None = None
        started = False
        try:
            with self._lock:
                if self._closed or uri in self._subscriptions:
                    return
                subscription = node.subscribe()
                close_callback = functools.partial(
                    self._handle_subscription_closed, uri, subscription
                )
                attach(subscription)
                subscription.stream_closed.add_callback(close_callback)
                self._subscriptions[uri] = (subscription, callback, close_callback)
                self._ordered_subscription_uris.append(uri)
                subscription.start_in_thread(self._start, max_size=self._max_size)
                started = True
        finally:
            if subscription is not None and not started:
                with self._lock:
                    managed_subscription = self._subscriptions.get(uri)
                    if (
                        managed_subscription is not None
                        and managed_subscription[0] is subscription
                    ):
                        self._subscriptions.pop(uri)
                        self._ordered_subscription_uris.remove(uri)
                try:
                    subscription.disconnect()
                except Exception:
                    logger.exception("Failed to disconnect Tiled subscription %s", uri)

    def _handle_subscription_closed(self, uri: str, subscription: Subscription) -> None:
        with self._lock:
            managed_subscription = self._subscriptions.get(uri)
            if (
                managed_subscription is None
                or managed_subscription[0] is not subscription
            ):
                return
            self._subscriptions.pop(uri)
            self._ordered_subscription_uris.remove(uri)

    def _handle_container_child(self, update: LiveChildCreated) -> None:
        child = update.child()
        if not isinstance(child, Container) or not _has_spec(child, "BlueskyRun"):
            return
        run_uid = child.item["id"]
        if self._run_filter is not None:
            try:
                if not self._run_filter(update):
                    return
            except Exception:
                logger.exception(
                    "Run filter failed for Tiled run %s at %s", run_uid, child.uri
                )
                return
        callback = functools.partial(self._handle_run_child, run_uid)
        self._subscribe_child_container(child, callback)

    def _handle_run_child(
        self,
        run_uid: str,
        update: LiveChildCreated,
    ) -> None:
        child = update.child()
        if not isinstance(child, Container) or not _has_spec(
            child, "BlueskyEventStream"
        ):
            return
        stream_name = child.item["id"]
        if self._streams is not None and stream_name not in self._streams:
            return
        callback = functools.partial(self._handle_stream_child, run_uid, stream_name)
        self._subscribe_child_container(child, callback)

    def _handle_stream_child(
        self,
        run_uid: str,
        stream_name: str,
        update: LiveChildCreated,
    ) -> None:
        child = update.child()
        item = child.item
        selected_data_keys = (
            None if self._streams is None else self._streams[stream_name]
        )
        structure_family = item["attributes"]["structure_family"]
        if structure_family == "array":
            data_key = item["id"]
            if selected_data_keys is not None and data_key not in selected_data_keys:
                return
            self._subscribe_child_data(child, run_uid, stream_name, (data_key,))
        elif structure_family == "table":
            columns = item["attributes"]["structure"]["columns"]
            data_keys = (
                tuple(key for key in columns if key in update.metadata)
                if selected_data_keys is None
                else tuple(key for key in selected_data_keys if key in columns)
            )
            if data_keys:
                self._subscribe_child_data(child, run_uid, stream_name, data_keys)

    def _subscribe_child_container(
        self,
        node: Container,
        callback: Callable[[LiveChildCreated], None],
    ) -> None:
        try:
            self._subscribe_container(node, callback)
        except Exception:
            logger.exception("Failed to subscribe to Tiled node %s", node.uri)

    def _subscribe_child_data(
        self,
        node: BaseClient,
        run_uid: str,
        stream_name: str,
        data_keys: tuple[str, ...],
    ) -> None:
        try:
            self._subscribe_data(
                cast(_Subscribable, node), run_uid, stream_name, data_keys
            )
        except Exception:
            logger.exception("Failed to subscribe to Tiled node %s", node.uri)

    def _handle_data_update(
        self,
        run_uid: str,
        stream_name: str,
        data_keys: tuple[str, ...],
        update: LiveArrayData | LiveArrayRef | LiveTableData,
    ) -> None:
        with self._lock:
            if self._closed:
                return
        self._callback(
            BlueskyStreamUpdate(
                run_uid=run_uid,
                stream_name=stream_name,
                data_keys=data_keys,
                update=update,
                sequence=update.sequence,
            )
        )


def _has_spec(node: BaseClient, name: str) -> bool:
    return any(spec["name"] == name for spec in node.item["attributes"]["specs"])


def _normalize_data_keys(
    data_keys: str | Iterable[str] | None,
) -> tuple[str, ...] | None:
    if data_keys is None:
        return None
    if isinstance(data_keys, str):
        return (data_keys,)
    return tuple(dict.fromkeys(data_keys))


def _normalize_streams(
    streams: Mapping[str, str | Iterable[str] | None] | None,
) -> dict[str, tuple[str, ...] | None] | None:
    if streams is None:
        return None
    if not streams:
        raise ValueError("At least one stream must be selected.")

    normalized = {}
    for stream_name, data_keys in streams.items():
        selected_data_keys = _normalize_data_keys(data_keys)
        if selected_data_keys == ():
            raise ValueError(
                f"At least one data key must be selected for stream {stream_name!r}."
            )
        normalized[stream_name] = selected_data_keys
    return normalized


def _compose_run_filter(
    metadata_filter: Callable[[Mapping[str, Any]], bool] | None,
    required_specs: str | Iterable[str] | None,
    run_filter: Callable[[LiveChildCreated], bool] | None,
) -> Callable[[LiveChildCreated], bool] | None:
    if required_specs is None:
        required = frozenset()
    elif isinstance(required_specs, str):
        required = frozenset((required_specs,))
    else:
        required = frozenset(required_specs)

    if metadata_filter is None and not required and run_filter is None:
        return None

    def composed(update: LiveChildCreated) -> bool:
        if metadata_filter is not None and not metadata_filter(
            update.metadata.get("start", _EMPTY_START_DOCUMENT)
        ):
            return False
        if required and not required <= {spec.name for spec in update.specs}:
            return False
        return run_filter is None or run_filter(update)

    return composed


def subscribe_to_streams(
    container: Container,
    callback: Callable[[BlueskyStreamUpdate], None],
    *,
    streams: Mapping[str, str | Iterable[str] | None] | None,
    metadata_filter: Callable[[Mapping[str, Any]], bool] | None = None,
    required_specs: str | Iterable[str] | None = None,
    run_filter: Callable[[LiveChildCreated], bool] | None = None,
    start: int | None = 0,
    max_size: int = 1_000_000,
) -> BlueskyStreamSubscription:
    """
    Subscribe to selected data keys in matching live Bluesky runs.

    Parameters
    ----------
    container : tiled.client.container.Container
        Direct parent of ``BlueskyRun`` nodes created by
        :class:`~bluesky_tiled_plugins.TiledWriter`.
    callback : Callable[[BlueskyStreamUpdate], None]
        Function called for each selected native Tiled live update.
    streams : Mapping[str, str or iterable of str or None] or None
        Mapping from Bluesky event-stream name to its data-key selection.
        A mapping value of ``None`` selects every streamable array and table key
        in that stream. An outer ``None`` selects every event stream and all its
        streamable array and table keys. Ragged and bytes nodes are ignored.
    metadata_filter : Callable[[Mapping[str, Any]], bool] or None, optional
        Predicate applied to the raw run-creation update's persisted
        ``metadata["start"]`` mapping.
    required_specs : str, iterable of str, or None, optional
        One spec name or every spec name required for a run to match.
    run_filter : Callable[[LiveChildCreated], bool] or None, optional
        Predicate applied to the raw Tiled run-creation update.
    start : int or None, optional
        Tiled sequence number supplied to every owned subscription. The default,
        ``0``, replays records retained by the streaming cache. ``None`` receives
        only new records.
    max_size : int, optional
        Maximum incoming WebSocket message size in bytes. Defaults to
        ``1_000_000``.

    Returns
    -------
    BlueskyStreamSubscription
        A running managed subscription. Use
        :meth:`~BlueskyStreamSubscription.disconnect` or a context manager to
        release it.

    Raises
    ------
    ValueError
        If ``streams`` is an empty mapping or any data-key selection is an
        explicit empty iterable.
    Exception
        Any error raised while Tiled establishes the root subscription.

    Notes
    -----
    The optional filters are evaluated in ``metadata_filter``,
    ``required_specs``, then ``run_filter`` order with short-circuiting AND
    semantics.

    The root container subscription is connected before this function returns.
    Updates from different data nodes or event streams have no global ordering,
    and Tiled may invoke the callback concurrently. Co-located selected table
    columns remain one native table update; this function does not join or align
    updates across nodes or streams.

    With Tiled 0.2.18, the server can stream ragged nodes, but
    ``RaggedClient`` has no subscription API and the client has no ragged live
    schema or update model. ``BytesClient`` likewise has no subscription API,
    and the server has no bytes live schema or cache emitter and rejects bytes
    on its single-node streaming route. Both families are ignored here.

    Examples
    --------
    >>> def on_update(update):
    ...     print(update.stream_name, update.data())
    ...
    >>> subscription = subscribe_to_streams(
    ...     container,
    ...     on_update,
    ...     streams={"baseline": ("x", "y"), "primary": None},
    ... )
    >>> subscription.disconnect()
    """
    normalized_streams = _normalize_streams(streams)
    return BlueskyStreamSubscription(
        container,
        normalized_streams,
        callback,
        start=start,
        max_size=max_size,
        run_filter=_compose_run_filter(metadata_filter, required_specs, run_filter),
    )

import functools
import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
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


@dataclass(frozen=True)
class BlueskyStreamUpdate:
    """
    A selected physical update from a Bluesky event stream.

    Parameters
    ----------
    run_uid : str
        UID of the direct ``BlueskyRun`` parent that produced the update.
    stream_name : str
        Name of the Bluesky event stream containing the updated node.
    data_keys : tuple of str
        Requested data keys represented by this physical update. A table update
        may contain several selected scalar keys; an array update contains one.
    update : LiveArrayData, LiveArrayRef, or LiveTableData
        Original Tiled live update. It is retained without decoding until
        :meth:`data` is called.
    """

    run_uid: str
    stream_name: str
    data_keys: tuple[str, ...]
    update: LiveArrayData | LiveArrayRef | LiveTableData

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
    """Own recursive Tiled subscriptions for one named Bluesky event stream."""

    def __init__(
        self,
        catalog: Container,
        stream_name: str,
        data_keys: tuple[str, ...],
        callback: Callable[[BlueskyStreamUpdate], None],
        *,
        start: int | None,
        max_size: int,
    ) -> None:
        """
        Create and start a managed stream subscription.

        Parameters
        ----------
        catalog : tiled.client.container.Container
            Direct parent of the ``BlueskyRun`` nodes created by the associated
            :class:`~bluesky_tiled_plugins.TiledWriter`.
        stream_name : str
            Name of the Bluesky event stream to observe, such as ``"primary"``
            or ``"baseline"``.
        data_keys : tuple of str
            Normalized, non-empty data-key selection. Use
            :func:`subscribe_to_stream` for the public input boundary.
        callback : Callable[[BlueskyStreamUpdate], None]
            Function called with each selected physical update. Tiled executes
            callbacks asynchronously; exceptions raised by this function are not
            caught by this manager.
        start : int or None
            Tiled sequence number supplied to every owned subscription. ``0``
            replays retained streaming-cache records; ``None`` receives only new
            records.
        max_size : int
            Maximum incoming WebSocket message size in bytes.

        Raises
        ------
        Exception
            Any error raised while Tiled establishes the root subscription. Any
            partially established owned subscriptions are disconnected first.

        Notes
        -----
        This constructor starts the root catalog subscription before returning.
        Prefer :func:`subscribe_to_stream`, which normalizes public key input and
        rejects an empty selection.
        """
        self._stream_name = stream_name
        self._data_keys = data_keys
        self._callback = callback
        self._start = start
        self._max_size = max_size
        self._lock = threading.Lock()
        self._closed = False
        self._subscriptions: dict[str, tuple[Subscription, Callable[..., None]]] = {}
        # URI keys in global parent-before-child creation order. Reversing this
        # linear extension of the hierarchy tears down leaves before parents.
        self._ordered_subscription_uris: list[str] = []

        established = False
        try:
            self._subscribe_container(catalog, self._handle_catalog_child)
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
        catalog subscriptions. This method is idempotent and blocks while Tiled
        closes sockets and waits for its subscription threads.

        Raises
        ------
        Exception
            The first error raised while disconnecting an owned Tiled
            subscription, after teardown is attempted for every owned
            subscription.

        Notes
        -----
        Call this from subscription-owner code, not from a callback delivered by
        this manager.
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
        data_keys: tuple[str, ...],
    ) -> None:
        callback = functools.partial(self._handle_data_update, run_uid, data_keys)

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
                attach(subscription)
                self._subscriptions[uri] = (subscription, callback)
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

    def _handle_catalog_child(self, update: LiveChildCreated) -> None:
        child = update.child()
        if not isinstance(child, Container) or not _has_spec(child, "BlueskyRun"):
            return
        callback = functools.partial(self._handle_run_child, child.item["id"])
        self._subscribe_child_container(child, callback)

    def _handle_run_child(self, run_uid: str, update: LiveChildCreated) -> None:
        child = update.child()
        if (
            not isinstance(child, Container)
            or not _has_spec(child, "BlueskyEventStream")
            or child.item["id"] != self._stream_name
        ):
            return
        callback = functools.partial(self._handle_stream_child, run_uid)
        self._subscribe_child_container(child, callback)

    def _handle_stream_child(self, run_uid: str, update: LiveChildCreated) -> None:
        child = update.child()
        item = child.item
        structure_family = item["attributes"]["structure_family"]
        if structure_family == "array":
            data_key = item["id"]
            if data_key not in self._data_keys:
                return
            self._subscribe_child_data(child, run_uid, (data_key,))
        elif structure_family == "table":
            columns = item["attributes"]["structure"]["columns"]
            data_keys = tuple(key for key in self._data_keys if key in columns)
            if data_keys:
                self._subscribe_child_data(child, run_uid, data_keys)

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
        data_keys: tuple[str, ...],
    ) -> None:
        try:
            self._subscribe_data(cast(_Subscribable, node), run_uid, data_keys)
        except Exception:
            logger.exception("Failed to subscribe to Tiled node %s", node.uri)

    def _handle_data_update(
        self,
        run_uid: str,
        data_keys: tuple[str, ...],
        update: LiveArrayData | LiveArrayRef | LiveTableData,
    ) -> None:
        with self._lock:
            if self._closed:
                return
        self._callback(
            BlueskyStreamUpdate(
                run_uid=run_uid,
                stream_name=self._stream_name,
                data_keys=data_keys,
                update=update,
            )
        )


def _has_spec(node: BaseClient, name: str) -> bool:
    return any(spec["name"] == name for spec in node.item["attributes"]["specs"])


def _normalize_data_keys(data_keys: str | Iterable[str]) -> tuple[str, ...]:
    if isinstance(data_keys, str):
        return (data_keys,)
    return tuple(dict.fromkeys(data_keys))


def subscribe_to_stream(
    catalog: Container,
    stream_name: str,
    data_keys: str | Iterable[str],
    callback: Callable[[BlueskyStreamUpdate], None],
    *,
    start: int | None = 0,
    max_size: int = 1_000_000,
) -> BlueskyStreamSubscription:
    """
    Subscribe to selected data keys in a named live Bluesky event stream.

    Parameters
    ----------
    catalog : tiled.client.container.Container
        Direct parent of ``BlueskyRun`` nodes created by
        :class:`~bluesky_tiled_plugins.TiledWriter`.
    stream_name : str
        Bluesky event-stream name to observe.
    data_keys : str or iterable of str
        Data key or keys to select. Iterable input is deduplicated while
        preserving its order.
    callback : Callable[[BlueskyStreamUpdate], None]
        Function called for each selected physical Tiled update.
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
        A running managed subscription. Use :meth:`~BlueskyStreamSubscription.disconnect`
        or a context manager to release it.

    Raises
    ------
    ValueError
        If ``data_keys`` normalizes to an empty selection.
    Exception
        Any error raised while Tiled establishes the root subscription.

    Notes
    -----
    Co-located selected table columns are delivered together in Tiled's decoded
    table representation. Arrays and columns in separate physical tables are
    delivered independently; this function does not join or align updates across
    nodes.

    Examples
    --------
    >>> def on_update(update):
    ...     print(update.data())
    ...
    >>> subscription = subscribe_to_stream(
    ...     catalog, "baseline", ("x", "y"), on_update
    ... )
    >>> subscription.disconnect()
    """
    selected_data_keys = _normalize_data_keys(data_keys)
    if not selected_data_keys:
        raise ValueError("At least one data key must be selected.")

    return BlueskyStreamSubscription(
        catalog,
        stream_name,
        selected_data_keys,
        callback,
        start=start,
        max_size=max_size,
    )

from __future__ import annotations

import inspect
import logging
import threading
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from tiled.client.stream import ContainerSubscription, Subscription, TableSubscription

    from ..clients.catalog_of_bluesky_runs import CatalogOfBlueskyRuns

logger = logging.getLogger(__name__)


def _matches_metadata_filters(
    metadata: dict[str, Any], filters: dict[str, Any]
) -> bool:
    """Check if a run's start metadata matches all filter key-value pairs."""
    start = metadata.get("start", {})
    return all(start.get(key) == value for key, value in filters.items())


def _validate_callback(
    callback: Callable[..., None], datasets: tuple[str, ...]
) -> None:
    """Validate that the callback accepts catalog as a positional arg and each
    dataset name as a keyword-only arg."""
    sig = inspect.signature(callback)
    params = list(sig.parameters.values())

    # Must have at least one positional parameter (for catalog)
    positional_kinds = (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )
    positional_params = [p for p in params if p.kind in positional_kinds]
    if not positional_params:
        raise TypeError(
            f"Callback {callback.__qualname__} must accept at least one positional "
            f"argument (for the catalog). Got signature: {sig}"
        )

    # Check that each dataset name is accepted as a keyword argument.
    # Allow **kwargs to satisfy any keyword name.
    has_var_keyword = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params
    )
    if not has_var_keyword:
        keyword_names = {
            p.name
            for p in params
            if p.kind
            in (
                inspect.Parameter.KEYWORD_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        }
        # Exclude the first positional param name from keyword set
        first_positional_name = positional_params[0].name
        keyword_names.discard(first_positional_name)

        missing = set(datasets) - keyword_names
        if missing:
            raise TypeError(
                f"Callback {callback.__qualname__} is missing keyword-only parameters "
                f"for datasets: {missing}. Got signature: {sig}"
            )


def _resolve_dataset_node(stream_client: Any, dataset_name: str) -> Any:
    """Resolve a dataset node from a stream client, handling V2/Mongo and
    V3/SQL layouts."""
    # V2/Mongo layout: stream["data"][name]
    try:
        if "data" in stream_client:
            data_container = stream_client["data"]
            if dataset_name in data_container:
                return data_container[dataset_name]
    except (KeyError, TypeError):
        pass

    # V3/SQL layout or fallback: stream[name]
    return stream_client[dataset_name]


class _RunSubscription:
    """Internal per-run subscription state.

    Subscribes to a run container, waits for the target stream to appear,
    then subscribes to data nodes within the stream.

    In V3/SQL layout, the stream's raw container has:
    - ``"internal"`` — a table node holding inline event data (scalars,
      small arrays).  The requested dataset columns are extracted from
      each table update.
    - Named array nodes — one per external data key (e.g. area-detector
      images registered via StreamResource / StreamDatum).

    Both kinds of children are handled transparently so the caller's
    callback receives the requested datasets regardless of storage path.
    """

    def __init__(
        self,
        catalog: CatalogOfBlueskyRuns,
        run_client: Any,
        stream_name: str,
        datasets: tuple[str, ...],
        callback: Callable[..., None],
        parent: CatalogSubscription,
        start: int | None = None,
    ) -> None:
        self._catalog = catalog
        self._run_client = run_client
        self._stream_name = stream_name
        self._datasets = datasets
        self._callback = callback
        self._parent = parent
        self._start = start

        self._lock = threading.Lock()
        self._pending: dict[str, Any] = {}
        self._subs: list[Subscription] = []
        # Keep strong references to callback handlers — tiled's
        # CallbackRegistry only holds weak references, so without
        # this the closures from _make_*_handler would be GC'd.
        self._handlers: list[Callable[..., None]] = []
        self._run_sub: ContainerSubscription | None = None
        self._stream_sub: ContainerSubscription | None = None
        self._stopped = False

    def start(self) -> None:
        """Subscribe to the run container and wait for the target stream."""
        self._run_sub = self._run_client.subscribe()
        self._run_sub.child_created.add_callback(self._on_stream_created)
        # Always use start=0 for inner subscriptions: the run was just
        # created so we need to catch the stream child_created event
        # which may have already been emitted by the time we connect.
        self._run_sub.start_in_thread(start=0)

    def _on_stream_created(self, update: Any) -> None:
        """Called when a child is created in the run container."""
        if update.key != self._stream_name:
            return

        # Access the stream via the run_client (proper HTTP navigation)
        # rather than update.child() which can produce broken path_parts
        # in subscription callbacks.
        from tiled.client.container import Container

        stream_node = self._run_client[self._stream_name]
        raw_stream = Container(
            stream_node.context,
            item=stream_node.item,
            structure_clients=stream_node.structure_clients,
        )
        self._raw_stream = raw_stream

        # Subscribe to the raw stream container to catch both the
        # "internal" table and any external array nodes as they appear.
        self._stream_sub = raw_stream.subscribe()
        self._stream_sub.child_created.add_callback(self._on_data_node_created)
        self._stream_sub.start_in_thread(start=0)

    def _on_data_node_created(self, update: Any) -> None:
        """Called when a child is created inside the raw stream container.

        This handles both the ``"internal"`` table (inline event data) and
        named external array nodes.
        """
        from tiled.structures.core import StructureFamily

        # Use update.child() here — the raw_stream has correct path_parts
        # so children inherit correct paths.  This also avoids race
        # conditions where HTTP navigation might fail if the node's
        # structure hasn't been fully committed yet.
        child = update.child()

        if (
            update.key == "internal"
            and child.structure_family == StructureFamily.table
        ):
            # The "internal" table holds inline event columns.  Subscribe
            # to it and extract only the columns we care about.
            columns_in_table = set(child.columns) if hasattr(child, "columns") else set()
            needed = set(self._datasets) & columns_in_table
            if not needed:
                return
            sub = child.subscribe()
            handler = self._make_table_handler(needed)
            self._handlers.append(handler)
            sub.new_data.add_callback(handler)
            sub.start_in_thread(start=0)
            self._subs.append(sub)

        elif update.key in self._datasets:
            # An external data node whose name matches a requested dataset.
            sub = child.subscribe()
            handler = self._make_array_handler(update.key)
            self._handlers.append(handler)
            sub.new_data.add_callback(handler)
            sub.start_in_thread(start=0)
            self._subs.append(sub)

    # ----- data handlers -----

    def _make_table_handler(
        self, needed_columns: set[str]
    ) -> Callable[..., None]:
        """Return a handler that extracts columns from table updates."""

        def handler(update: Any) -> None:
            df = update.data()
            for col in needed_columns:
                if col in df.columns:
                    values = df[col].to_numpy()
                    self._store_and_maybe_fire(col, values)

        return handler

    def _make_array_handler(self, dataset_name: str) -> Callable[..., None]:
        """Return a handler for an external array node."""

        def handler(update: Any) -> None:
            self._store_and_maybe_fire(dataset_name, update.data())

        return handler

    def _store_and_maybe_fire(self, name: str, data: Any) -> None:
        """Store a dataset value and fire the callback if all are ready."""
        fire = False
        kwargs: dict[str, Any] = {}
        with self._lock:
            if self._stopped:
                return
            self._pending[name] = data
            if len(self._pending) == len(self._datasets):
                kwargs = dict(self._pending)
                self._pending.clear()
                fire = True
        if fire:
            try:
                self._callback(self._catalog, **kwargs)
            except Exception:
                logger.exception(
                    "Error in streaming callback for datasets %s",
                    self._datasets,
                )

    # ----- lifecycle -----

    def stop(self) -> None:
        """Disconnect all subscriptions for this run."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True

        for sub in self._subs:
            try:
                sub.disconnect()
            except Exception:
                logger.debug("Error disconnecting data subscription", exc_info=True)
        self._subs.clear()

        if self._stream_sub is not None:
            try:
                self._stream_sub.disconnect()
            except Exception:
                logger.debug("Error disconnecting stream subscription", exc_info=True)
            self._stream_sub = None

        if self._run_sub is not None:
            try:
                self._run_sub.disconnect()
            except Exception:
                logger.debug("Error disconnecting run subscription", exc_info=True)
            self._run_sub = None

        self._parent._remove_run_sub(self)


class CatalogSubscription:
    """Manages streaming subscriptions for new runs in a catalog.

    Watches for new runs via the catalog's websocket subscription. When a run
    matching the metadata filters appears, it automatically subscribes to the
    specified datasets within the given stream and fires the callback with
    synchronized updates.

    The callback receives the catalog as a positional argument and each
    dataset's latest data as keyword arguments::

        def my_callback(
            catalog: CatalogOfBlueskyRuns,
            *,
            det: numpy.ndarray,
            motor: numpy.ndarray,
        ) -> None:
            ...

    Parameters
    ----------
    catalog : CatalogOfBlueskyRuns
        The catalog to watch for new runs.
    stream_name : str
        Name of the stream within each run (e.g. ``"primary"``).
    datasets : tuple[str, ...]
        Names of datasets to subscribe to within the stream.
    callback : Callable
        Function called with ``(catalog, **{dataset_name: data, ...})``
        when all datasets have new data.
    start : int or None, optional
        Sequence number to start from. ``None`` means only new updates,
        ``0`` means from the beginning.
    metadata_filters : dict[str, Any]
        Key-value pairs matched against ``run.start`` metadata. All must
        match (AND logic). Pass none to accept all runs.
    """

    def __init__(
        self,
        catalog: CatalogOfBlueskyRuns,
        stream_name: str,
        datasets: tuple[str, ...],
        callback: Callable[..., None],
        start: int | None = None,
        metadata_filters: dict[str, Any] | None = None,
    ) -> None:
        self._catalog = catalog
        self._stream_name = stream_name
        self._datasets = datasets
        self._callback = callback
        self._start = start
        self._metadata_filters = metadata_filters or {}

        self._catalog_sub: ContainerSubscription | None = None
        self._run_subs_lock = threading.Lock()
        self._run_subs: list[_RunSubscription] = []
        self._stopped = False

    def start(self, start: int | None = None) -> None:
        """Start the catalog subscription, blocking the calling thread.

        Parameters
        ----------
        start : int or None, optional
            Override the sequence number to start from for the catalog
            subscription. If ``None``, uses the value from construction.
        """
        seq = start if start is not None else self._start
        self._catalog_sub = self._catalog.subscribe()
        self._catalog_sub.child_created.add_callback(self._on_run_created)
        self._catalog_sub.start(start=seq)

    def start_in_thread(self, start: int | None = None) -> CatalogSubscription:
        """Start the catalog subscription in a background thread.

        Parameters
        ----------
        start : int or None, optional
            Override the sequence number to start from for the catalog
            subscription. If ``None``, uses the value from construction.

        Returns
        -------
        CatalogSubscription
            Returns self for use as a context manager.
        """
        seq = start if start is not None else self._start
        self._catalog_sub = self._catalog.subscribe()
        self._catalog_sub.child_created.add_callback(self._on_run_created)
        self._catalog_sub.start_in_thread(start=seq)
        return self

    def _on_run_created(self, update: Any) -> None:
        """Called when a new run is created in the catalog."""
        if self._metadata_filters and not _matches_metadata_filters(
            update.metadata, self._metadata_filters
        ):
            return

        # Navigate to the run via the catalog client (proper HTTP path)
        # rather than update.child() which produces clients with broken
        # path_parts in subscription callbacks.
        run_client = self._catalog[update.key]
        run_sub = _RunSubscription(
            catalog=self._catalog,
            run_client=run_client,
            stream_name=self._stream_name,
            datasets=self._datasets,
            callback=self._callback,
            parent=self,
            start=self._start,
        )
        with self._run_subs_lock:
            self._run_subs.append(run_sub)
        run_sub.start()

    def _remove_run_sub(self, run_sub: _RunSubscription) -> None:
        """Remove a run subscription from the active list."""
        with self._run_subs_lock:
            try:
                self._run_subs.remove(run_sub)
            except ValueError:
                pass

    def stop(self) -> None:
        """Stop the catalog subscription and all active run subscriptions."""
        self._stopped = True
        with self._run_subs_lock:
            run_subs = list(self._run_subs)
        for run_sub in run_subs:
            run_sub.stop()

        if self._catalog_sub is not None:
            try:
                self._catalog_sub.disconnect()
            except Exception:
                logger.debug(
                    "Error disconnecting catalog subscription", exc_info=True
                )
            self._catalog_sub = None

    def __enter__(self) -> CatalogSubscription:
        return self

    def __exit__(self, *args: Any) -> None:
        self.stop()


def subscribe_catalog(
    catalog: CatalogOfBlueskyRuns,
    stream_name: str,
    datasets: tuple[str, ...],
    callback: Callable[..., None],
    *,
    start: int | None = None,
    **metadata_filters: Any,
) -> CatalogSubscription:
    """Subscribe to synchronized dataset updates from new runs in a catalog.

    Sets up a cascading subscription chain: catalog -> run -> stream ->
    datasets. When all specified datasets within a stream have new data,
    the callback is fired with the catalog as a positional argument and
    each dataset's data as a keyword argument.

    Parameters
    ----------
    catalog : CatalogOfBlueskyRuns
        The catalog to watch for new runs.
    stream_name : str
        Name of the stream within each run (e.g. ``"primary"``).
    datasets : tuple[str, ...]
        Names of datasets to subscribe to within the stream.
    callback : Callable
        Function called as ``callback(catalog, **{name: data, ...})``.
        Must accept the catalog as a positional argument and each dataset
        name as a keyword-only argument.
    start : int or None, optional
        Sequence number to start from. ``None`` means only new updates,
        ``0`` means from the beginning.
    **metadata_filters : Any
        Key-value pairs matched against ``run.start`` metadata (AND logic).
        For example, ``plan_name="count"`` only subscribes to runs whose
        start document has ``plan_name`` equal to ``"count"``.

    Returns
    -------
    CatalogSubscription
        A handle to manage the subscription. Call ``.start_in_thread()``
        to begin receiving updates in the background, or ``.start()`` to
        block. Use as a context manager or call ``.stop()`` to clean up.

    Examples
    --------
    >>> def on_data(catalog, *, det, motor):
    ...     print(f"det={det}, motor={motor}")
    ...
    >>> sub = subscribe_catalog(
    ...     catalog, "primary", ("det", "motor"), on_data,
    ...     plan_name="count",
    ... )
    >>> sub.start_in_thread()
    >>> # ... later ...
    >>> sub.stop()
    """
    _validate_callback(callback, datasets)
    return CatalogSubscription(
        catalog=catalog,
        stream_name=stream_name,
        datasets=datasets,
        callback=callback,
        start=start,
        metadata_filters=metadata_filters,
    )

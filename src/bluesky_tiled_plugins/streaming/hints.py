"""Subscribe to the hinted fields of live Bluesky event streams in Tiled."""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import pandas as pd
from .streaming import BlueskyStreamSubscription, BlueskyStreamUpdate, subscribe_to_streams
from tiled.client.container import Container
from tiled.client.stream import LiveChildCreated, LiveTableData

# Columns that TiledWriter adds to every internal table row.
SEQ_NUM = "seq_num"
TIME = "time"


def _hinted_fields(descriptor: dict[str, Any]) -> list[str]:
    """Return the hinted data keys of a stream, mirroring ``bluesky.callbacks.best_effort``.

    TiledWriter drops ``object_keys`` from the stream metadata but records each
    key's ``object_name``, so objects are reconstructed from ``data_keys``.
    """
    data_keys = descriptor.get("data_keys", {})
    hints = descriptor.get("hints", {}) or {}
    by_object: dict[str, list[str]] = {}
    for key, spec in data_keys.items():
        by_object.setdefault(spec.get("object_name", key), []).append(key)

    fields: list[str] = []
    for obj, keys in by_object.items():
        obj_fields = hints[obj].get("fields", []) if obj in hints else keys
        fields.extend(f for f in obj_fields if f in data_keys and f not in fields)
    return fields


def _dimensions(start: dict[str, Any]) -> list[tuple[list[str], str]]:
    """Return the plan's ``(fields, stream_name)`` dimensions with BEC's defaults."""
    dims = (start.get("hints") or {}).get("dimensions")
    if dims:
        return [(list(fields), stream) for fields, stream in dims]
    if motors := start.get("motors"):
        return [([motor], "primary") for motor in motors]
    return [([TIME], "primary")]


def _is_scalar(spec: dict[str, Any]) -> bool:
    """Whether a data key holds internal scalar readings."""
    return "external" not in spec and not spec.get("shape")


@dataclass(frozen=True)
class HintedStreamUpdate:
    """New rows of a Bluesky event stream together with its hints.

    Parameters
    ----------
    run_uid : str
        Uid of the run, the key of its ``BlueskyRun`` node.
    stream_name : str
        Name of the event stream.
    start : dict
        The run's start document.
    descriptor : dict
        The event stream node's metadata (the descriptor as written by TiledWriter).
    fields : tuple of str
        Scalar dimension fields of this stream followed by its hinted fields.
    table : pandas.DataFrame
        All new rows, with every column TiledWriter wrote (``seq_num``, ``time``,
        data keys, and ``ts_*`` timestamps).
    """

    run_uid: str
    stream_name: str
    start: dict[str, Any]
    descriptor: dict[str, Any]
    fields: tuple[str, ...]
    table: pd.DataFrame

    def data(self) -> pd.DataFrame:
        """Return the ``seq_num``, ``time``, and hinted columns of the new rows."""
        return self.table.loc[:, [SEQ_NUM, TIME, *self.fields]]


def subscribe_to_hinted_streams(
    container: Container,
    callback: Callable[[HintedStreamUpdate], None],
    *,
    run_filter: Callable[[LiveChildCreated], bool] | None = None,
    on_run_stop: Callable[[str, Mapping[str, Any]], None] | None = None,
    retained_runs: int = 4,
    start: int | None = 0,
    max_size: int = 1_000_000,
) -> BlueskyStreamSubscription:
    """Subscribe to live table data of every event stream, annotated with its hints.

    Hints are only known once a run and its streams exist, so every stream is
    subscribed and each stream's descriptor is fetched from Tiled on its first
    update. Only TiledWriter's internal tables are delivered; array nodes are ignored.

    Parameters
    ----------
    container : tiled.client.container.Container
        Direct parent of the ``BlueskyRun`` nodes created by ``TiledWriter``.
    callback : Callable[[HintedStreamUpdate], None]
        Called on a Tiled worker thread, possibly concurrently, for each table update.
    run_filter : Callable[[LiveChildCreated], bool], optional
        Predicate applied to each new run. It runs before any of the run's data is delivered.
    on_run_stop : Callable[[str, Mapping], None], optional
        Called with the run uid and stop document of each accepted run. It may
        precede the run's last data updates; see ``subscribe_to_streams``.
    retained_runs : int, optional
        Number of stopped runs whose start document and descriptors stay cached.
        Data arriving for a run after its release is still delivered, but
        refetches the start document and descriptor from Tiled on every update.
    start, max_size
        Passed through to ``subscribe_to_streams``.
    """
    if retained_runs < 0:
        raise ValueError(f"retained_runs must be non-negative, not {retained_runs}.")
    lock = threading.Lock()
    starts: dict[str, dict[str, Any]] = {}
    streams: dict[tuple[str, str], tuple[dict[str, Any], tuple[str, ...]]] = {}
    stopped: deque[str] = deque()

    def accept_run(update: LiveChildCreated) -> bool:
        if run_filter is not None and not run_filter(update):
            return False
        with lock:
            starts[update.key] = dict(update.metadata.get("start", {}))
        return True

    def stream_info(
        run_uid: str, stream_name: str
    ) -> tuple[dict[str, Any], dict[str, Any], tuple[str, ...]]:
        with lock:
            key = (run_uid, stream_name)
            if key in streams:
                return (starts[run_uid], *streams[key])
            run = container[run_uid]
            run = getattr(run, "base", run)
            # A missing start means the run was already released; don't re-cache it.
            released = run_uid not in starts
            start_doc = dict(run.metadata.get("start", {})) if released else starts[run_uid]
            descriptor = dict(run[stream_name].metadata)
            data_keys = descriptor.get("data_keys", {})
            dim_fields = [f[0] for f, name in _dimensions(start_doc) if name == stream_name]
            fields = tuple(
                dict.fromkeys(
                    f
                    for f in (*dim_fields, *_hinted_fields(descriptor))
                    if f in data_keys and _is_scalar(data_keys[f])
                )
            )
            if not released:
                streams[key] = (descriptor, fields)
            return start_doc, descriptor, fields

    def on_update(update: BlueskyStreamUpdate) -> None:
        if not isinstance(update.update, LiveTableData):
            return
        start_doc, descriptor, fields = stream_info(update.run_uid, update.stream_name)
        callback(
            HintedStreamUpdate(
                run_uid=update.run_uid,
                stream_name=update.stream_name,
                start=start_doc,
                descriptor=descriptor,
                fields=fields,
                # The raw update keeps seq_num and time, which BlueskyStreamUpdate.data() drops.
                table=update.update.data(),
            )
        )

    def run_stopped(run_uid: str, stop: Mapping[str, Any]) -> None:
        with lock:
            stopped.append(run_uid)
            while len(stopped) > retained_runs:
                released = stopped.popleft()
                starts.pop(released, None)
                for key in [key for key in streams if key[0] == released]:
                    del streams[key]
        if on_run_stop is not None:
            on_run_stop(run_uid, stop)

    return subscribe_to_streams(
        container,
        on_update,
        streams=None,
        run_filter=accept_run,
        on_run_stop=run_stopped,
        start=start,
        max_size=max_size,
    )

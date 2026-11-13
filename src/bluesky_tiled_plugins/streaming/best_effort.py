"""A best-effort callback that consumes Tiled live streams instead of Bluesky documents."""

from __future__ import annotations

import logging
import queue
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
from tiled.client.container import Container
from tiled.client.stream import LiveChildCreated

from .hints import SEQ_NUM as _SEQ_NUM
from .hints import TIME as _TIME
from .hints import HintedStreamUpdate, _dimensions, subscribe_to_hinted_streams

logger = logging.getLogger(__name__)


def peak_stats(x: np.ndarray, y: np.ndarray) -> dict[str, Any]:
    """Coarse peak statistics (max, min, com, cen, fwhm) of ``y`` versus ``x``."""
    i_max, i_min = int(np.argmax(y)), int(np.argmin(y))
    total = float(np.sum(y))
    half = y[i_min] + (y[i_max] - y[i_min]) / 2
    above = np.flatnonzero(y >= half)
    left, right = x[above[0]], x[above[-1]]
    return {
        "max": (x[i_max], y[i_max]),
        "min": (x[i_min], y[i_min]),
        "com": float(np.sum(x * y) / total) if total else np.nan,
        "cen": (left + right) / 2,
        "fwhm": abs(right - left),
    }


class _TablePrinter:
    """Text table laid out like ``bluesky.callbacks.LiveTable``."""

    def __init__(
        self,
        columns: list[str],
        data_keys: dict[str, Any],
        out: Callable[[str], None],
        min_width: int = 12,
    ):
        self.columns = columns
        self.data_keys = data_keys
        self.out = out
        # LiveTable: seq_num is 10 wide with a trailing pad; other columns are padded on both sides.
        self.widths = [10] + [max(min_width - 2, len(c)) for c in (_TIME, *columns)]
        self.border = "+" + "-" * 11 + "".join("+" + "-" * (w + 2) for w in self.widths[1:]) + "+"

    def _line(self, cells: Iterable[str]) -> str:
        first, *rest = cells
        return f"|{first:>{self.widths[0]}} |" + "".join(
            f" {c:>{w}} |" for c, w in zip(rest, self.widths[1:])
        )

    def header(self) -> None:
        self.out(self.border)
        self.out(self._line((_SEQ_NUM, _TIME, *self.columns)))
        self.out(self.border)

    def row(self, row: pd.Series) -> None:
        when = datetime.fromtimestamp(row[_TIME]).strftime("%H:%M:%S.%f")[:10]
        cells = [str(int(row[_SEQ_NUM])), when, *(self._format(c, row.get(c)) for c in self.columns)]
        self.out(self._line(cells))

    def footer(self) -> None:
        self.out(self.border)

    def _format(self, key: str, value: Any) -> str:
        if value is None or (isinstance(value, float) and np.isnan(value)):
            return ""
        if isinstance(value, (float, np.floating)):
            precision = self.data_keys.get(key, {}).get("precision")
            return f"{value:.{3 if precision is None else int(precision)}f}"
        return str(value)


class _LinePlotter:
    """1D live plot of several y fields against one x field, like ``LivePlot``."""

    def __init__(self, fig_name: str, x_key: str, y_keys: list[str], label: str, x_offset: float):
        import matplotlib.pyplot as plt

        self.x_key, self.y_keys, self.x_offset = x_key, y_keys, x_offset
        self.fig = plt.figure(fig_name)
        # Overplot on an existing figure with the same layout, as BEC does by default.
        if len(self.fig.axes) != len(y_keys):
            self.fig.clf()
            self.fig.subplots(len(y_keys), 1, sharex=True, squeeze=False)
        self.axes = self.fig.axes
        self.x: list[float] = []
        self.y: dict[str, list[float]] = {k: [] for k in y_keys}
        self.lines = {}
        for ax, key in zip(self.axes, y_keys):
            ax.set_ylabel(key)
            (self.lines[key],) = ax.plot([], [], "o-", label=label)
            ax.legend(loc="best")
        self.axes[-1].set_xlabel(x_key)

    def update(self, df: pd.DataFrame) -> None:
        self.x.extend((df[self.x_key] - self.x_offset).tolist())
        for key in self.y_keys:
            self.y[key].extend(df[key].tolist())
            self.lines[key].set_data(self.x, self.y[key])
        for ax in self.axes:
            ax.relim()
            ax.autoscale_view()
        self.fig.canvas.draw_idle()

    def peaks(self) -> dict[str, dict[str, Any]]:
        x = np.asarray(self.x, dtype=float)
        return {key: peak_stats(x, np.asarray(self.y[key], dtype=float)) for key in self.y_keys if self.y[key]}


class _ScatterPlotter:
    """2D live scatter of y fields colored by value, like ``LiveScatter``."""

    def __init__(self, fig_name: str, x_key: str, y_key: str, z_keys: list[str], label: str):
        import matplotlib.pyplot as plt

        self.x_key, self.y_key, self.z_keys = x_key, y_key, z_keys
        self.fig = plt.figure(fig_name)
        self.fig.clf()
        axes = self.fig.subplots(1, len(z_keys), squeeze=False)[0]
        self.xy: list[tuple[float, float]] = []
        self.z: dict[str, list[float]] = {k: [] for k in z_keys}
        self.collections = {}
        for ax, key in zip(axes, z_keys):
            self.collections[key] = ax.scatter([], [], c=[], cmap="viridis")
            ax.set(title=f"{key} [{label}]", xlabel=x_key, ylabel=y_key)
            self.fig.colorbar(self.collections[key], ax=ax)

    def update(self, df: pd.DataFrame) -> None:
        self.xy.extend(zip(df[self.x_key].tolist(), df[self.y_key].tolist()))
        offsets = np.asarray(self.xy, dtype=float)
        for key, collection in self.collections.items():
            self.z[key].extend(df[key].tolist())
            values = np.asarray(self.z[key], dtype=float)
            collection.set_offsets(offsets)
            collection.set_array(values)
            collection.set_clim(np.nanmin(values), np.nanmax(values))
            collection.axes.update_datalim(offsets)
            collection.axes.autoscale_view()
        self.fig.canvas.draw_idle()


@dataclass
class _StreamState:
    name: str
    descriptor: dict[str, Any]
    rows: int = 0
    table: _TablePrinter | None = None
    plotter: _LinePlotter | _ScatterPlotter | None = None


@dataclass
class _RunState:
    uid: str
    start: dict[str, Any]
    streams: dict[str, _StreamState] = field(default_factory=dict)
    stop: dict[str, Any] | None = None
    stop_received: float = 0.0
    baseline_readings: int = 0
    # BEC holds the end-of-run baseline until the run's table is closed.
    end_baseline: list[str] = field(default_factory=list)


class TiledBestEffortStreamer:
    """Best-effort visualization of Bluesky runs streamed live from Tiled.

    Unlike ``bluesky.callbacks.best_effort.BestEffortCallback``, this is not
    subscribed to a RunEngine. It subscribes to a Tiled container that a
    ``TiledWriter`` writes into and reconstructs the heading, live table,
    live plots, baseline readings, and peak statistics from:

    * the run node creation (``metadata["start"]``),
    * each event stream node's metadata (the descriptor) and its live table data,
    * the run node metadata update that adds ``metadata["stop"]``.

    Tiled invokes callbacks on worker threads, so updates are queued and only
    rendered when :meth:`process_events` (or :meth:`run_forever`) is called,
    which must happen on the thread that owns the matplotlib GUI.

    Parameters
    ----------
    container : tiled.client.container.Container
        Direct parent of the ``BlueskyRun`` nodes created by ``TiledWriter``.
    noplot_streams : iterable of str
        Streams that get neither a live table nor a plot.
    replay : bool
        Also show runs whose start time precedes this callback's connection and
        that are still retained in Tiled's streaming cache.
    stop_timeout : float
        Seconds to wait after the stop arrives for the remaining stream data
        before finishing the run.
    retained_runs : int
        Stopped runs whose start and descriptors stay cached; see
        ``subscribe_to_hinted_streams``.
    out : callable
        Receives each line of text output. Defaults to ``print``.
    """

    def __init__(
        self,
        container: Container,
        *,
        noplot_streams: Iterable[str] = ("baseline",),
        replay: bool = False,
        stop_timeout: float = 2.0,
        retained_runs: int = 4,
        max_size: int = 1_000_000,
        out: Callable[[str], None] = print,
    ):
        self._container = container
        self.noplot_streams = list(noplot_streams)
        self._replay = replay
        self._stop_timeout = stop_timeout
        self._retained_runs = retained_runs
        self._max_size = max_size
        self._out = out
        self._queue: queue.Queue[tuple[Any, ...]] = queue.Queue()
        # Runs are rendered one at a time; updates for later runs wait in _deferred.
        self._run: _RunState | None = None
        self._deferred: dict[str, list[tuple[Any, ...]]] = {}
        self._data_subscription = None
        self._connected_at = 0.0
        self._heading_enabled = True
        self._table_enabled = True
        self._baseline_enabled = True
        self._plots_enabled = True
        self.peaks: dict[str, dict[str, Any]] = {}

    # -- BEC-compatible toggles ------------------------------------------------

    def enable_heading(self) -> None:
        self._heading_enabled = True

    def disable_heading(self) -> None:
        self._heading_enabled = False

    def enable_table(self) -> None:
        self._table_enabled = True

    def disable_table(self) -> None:
        self._table_enabled = False

    def enable_baseline(self) -> None:
        self._baseline_enabled = True

    def disable_baseline(self) -> None:
        self._baseline_enabled = False

    def enable_plots(self) -> None:
        self._plots_enabled = True

    def disable_plots(self) -> None:
        self._plots_enabled = False

    # -- Subscription lifecycle ------------------------------------------------

    def start(self) -> TiledBestEffortStreamer:
        """Connect the Tiled websocket subscriptions."""
        self._connected_at = time.time()
        self._data_subscription = subscribe_to_hinted_streams(
            self._container,
            self._on_data,
            run_filter=self._on_run_created,
            on_run_stop=self._on_run_stop,
            retained_runs=self._retained_runs,
            max_size=self._max_size,
        )
        return self

    def disconnect(self) -> None:
        """Disconnect all Tiled subscriptions."""
        if self._data_subscription is not None:
            self._data_subscription.disconnect()
            self._data_subscription = None

    def __enter__(self) -> TiledBestEffortStreamer:
        return self.start()

    def __exit__(self, *args: object) -> None:
        self.disconnect()

    # -- Tiled worker-thread callbacks: only enqueue -----------------------------

    def _on_run_created(self, update: LiveChildCreated) -> bool:
        start = dict(update.metadata.get("start", {}))
        if not self._replay and start.get("time", 0.0) < self._connected_at:
            return False
        # Enqueued before subscribe_to_streams subscribes to the run's children,
        # so the start always precedes the run's data in the queue.
        self._queue.put(("start", update.key, start))
        return True

    def _on_data(self, update: HintedStreamUpdate) -> None:
        self._queue.put(("data", update))

    def _on_run_stop(self, run_uid: str, stop: Mapping[str, Any]) -> None:
        self._queue.put(("stop", run_uid, dict(stop)))

    # -- Main-thread processing ----------------------------------------------------

    def process_events(self, timeout: float = 0.0) -> None:
        """Render queued Tiled updates, waiting up to ``timeout`` seconds for more."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            try:
                event = self._queue.get(timeout=remaining) if remaining > 0 else self._queue.get_nowait()
            except queue.Empty:
                break
            self._dispatch(event)
        self._finish_stopped_run()

    def _dispatch(self, event: tuple[Any, ...]) -> None:
        kind = event[0]
        run_uid = event[1].run_uid if kind == "data" else event[1]
        active = self._run.uid if self._run is not None else None
        if run_uid != active and not (active is None and kind == "start" and not self._deferred):
            # Updates for runs that were filtered out never have a start and are dropped.
            if kind == "start" or run_uid in self._deferred:
                self._deferred.setdefault(run_uid, []).append(event)
            return
        try:
            getattr(self, f"_handle_{kind}")(*event[1:])
        except Exception:
            logger.exception("Failed to process Tiled update %r", kind)

    def run_forever(self, interval: float = 0.1) -> None:
        """Process updates and keep matplotlib figures responsive until interrupted."""
        import matplotlib.pyplot as plt

        plt.ion()
        try:
            while True:
                self.process_events(timeout=interval)
                for num in plt.get_fignums():
                    plt.figure(num).canvas.flush_events()
        except KeyboardInterrupt:
            pass

    def _handle_start(self, run_uid: str, start: dict[str, Any]) -> None:
        self._run = _RunState(run_uid, start)
        if self._heading_enabled:
            when = datetime.fromtimestamp(start.get("time", time.time()))
            self._out("")
            self._out(f"Transient Scan ID: {start.get('scan_id')}     Time: {when:%Y-%m-%d %H:%M:%S}")
            self._out(f"Persistent Unique Scan ID: '{run_uid}'")

    def _handle_data(self, update: HintedStreamUpdate) -> None:
        run = self._run
        stream = run.streams.get(update.stream_name)
        if stream is None:
            stream = run.streams[update.stream_name] = self._new_stream(run, update)

        df = update.table
        stream.rows += len(df)
        if stream.name == "baseline":
            self._print_baseline(run, stream, df)
        if stream.table is not None:
            for _, row in df.iterrows():
                stream.table.row(row)
        if stream.plotter is not None:
            stream.plotter.update(df)

    def _handle_stop(self, run_uid: str, stop: dict[str, Any]) -> None:
        self._run.stop, self._run.stop_received = stop, time.monotonic()

    def _new_stream(self, run: _RunState, update: HintedStreamUpdate) -> _StreamState:
        stream_name = update.stream_name
        stream = _StreamState(stream_name, update.descriptor)
        if self._heading_enabled:
            self._out(f"New stream: {stream_name!r}")
        if stream_name in self.noplot_streams:
            return stream

        data_keys = update.descriptor.get("data_keys", {})
        columns = list(update.fields)
        dim_fields = [fields[0] for fields, name in _dimensions(run.start) if name == stream_name]

        if self._table_enabled:
            stream.table = _TablePrinter(columns, data_keys, self._out)
            stream.table.header()
        if self._plots_enabled and dim_fields:
            stream.plotter = self._make_plotter(run, dim_fields, [c for c in columns if c not in dim_fields])
        return stream

    def _make_plotter(self, run: _RunState, dim_fields: list[str], y_keys: list[str]):
        if not y_keys:
            return None
        import matplotlib.pyplot as plt

        plt.ion()
        label = str(run.start.get("scan_id", run.uid[:8]))
        if len(dim_fields) == 1:
            x_key = dim_fields[0]
            x_offset = run.start.get("time", 0.0) if x_key == _TIME else 0.0
            return _LinePlotter(f"{' '.join(sorted(y_keys))} vs {x_key}", x_key, y_keys, label, x_offset)
        if len(dim_fields) == 2:
            x_key, y_key = dim_fields
            return _ScatterPlotter(f"{' '.join(sorted(y_keys))} vs {x_key} {y_key}", x_key, y_key, y_keys, label)
        return None

    def _print_baseline(self, run: _RunState, stream: _StreamState, df: pd.DataFrame) -> None:
        if not self._baseline_enabled:
            return
        data_keys = stream.descriptor.get("data_keys", {})
        keys = [k for k in df.columns if k in data_keys]
        border = "+" + "-" * 32 + "+" + "-" * 32 + "+"
        for _, row in df.iterrows():
            run.baseline_readings += 1
            if run.baseline_readings == 1:
                lines, out = ["Start-of-run baseline readings:"], self._out
            else:
                run.end_baseline.clear()
                lines, out = ["End-of-run baseline readings:"], run.end_baseline.append
            lines.append(border)
            lines.extend(f"| {key:>30} | {row[key]!s:<30} |" for key in keys)
            lines.append(border)
            for line in lines:
                out(line)

    def _finish_stopped_run(self) -> None:
        while self._run is not None and self._run.stop is not None:
            run = self._run
            num_events = run.stop.get("num_events", {}) or {}
            complete = all(
                name in run.streams and run.streams[name].rows >= n for name, n in num_events.items() if n
            )
            if not complete and time.monotonic() - run.stop_received < self._stop_timeout:
                return
            self._finish_run(run)
            self._run = None
            if self._deferred:
                for event in self._deferred.pop(next(iter(self._deferred))):
                    self._dispatch(event)

    def _finish_run(self, run: _RunState) -> None:
        for stream in run.streams.values():
            if stream.table is not None:
                stream.table.footer()
            if isinstance(stream.plotter, _LinePlotter):
                self.peaks = stream.plotter.peaks()
        if self._table_enabled:
            plan = run.start.get("plan_name", "")
            self._out(f"generator {plan} ['{run.uid[:8]}'] (scan num: {run.start.get('scan_id')})")
        for line in run.end_baseline:
            self._out(line)
        status = run.stop.get("exit_status")
        if status != "success":
            self._out(f"Run {run.uid[:8]} exited with status {status!r}: {run.stop.get('reason', '')}")

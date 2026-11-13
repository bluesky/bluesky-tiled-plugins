# Visualize Live Bluesky Runs

`TiledBestEffortStreamer` reproduces the output of
`bluesky.callbacks.best_effort.BestEffortCallback` — heading, live table,
baseline readings, live plots, and peak statistics — from Tiled live streams
instead of a RunEngine subscription. It works wherever the catalog is reachable:
in the acquisition process, or in a separate UI process while plans run under a
Queue Server.

```python
from bluesky_tiled_plugins.streaming import TiledBestEffortStreamer

with TiledBestEffortStreamer(container) as bec:
    bec.run_forever()
```

`container` is the direct parent `Container` of the `BlueskyRun` nodes that a
`TiledWriter` writes into, as in the [streaming tutorial](./streaming.md). Set
`batch_size=1` on the writer so each Event is delivered as it is taken, and
enable the server's streaming cache per the
[deployment guide](../how-to/deploy.md).

Live plots require matplotlib, available through the `plotting` extra:

```
pip install "bluesky-tiled-plugins[plotting]"
```

Without it, plots are disabled with a warning, but the text output still works.

## Render on the right thread

Tiled delivers updates on worker threads, so the streamer only queues them;
nothing is rendered until the thread that owns matplotlib asks for it. Use
whichever fits the application:

- `run_forever(interval=0.1)` blocks, rendering updates and keeping the figures
  responsive until `KeyboardInterrupt`. Use this in a dedicated console.
- `process_events(timeout=0.0)` renders everything queued and returns. Call it
  periodically from an existing event loop or GUI timer, or after `RE(plan)`
  when running in-process:

```python
from bluesky_tiled_plugins import TiledWriter

RE.subscribe(TiledWriter(client, batch_size=1))
bec = TiledBestEffortStreamer(client).start()

RE(scan([det], motor, -1, 1, 5))
bec.process_events(timeout=1.0)
```

## BEC-compatible behavior

The familiar toggles and results work the same way:

```python
bec.disable_table()
bec.enable_plots()
bec.noplot_streams.append("monitor")

bec.peaks["cen"]["det"]   # like BestEffortCallback.peaks
bec.peaks.fwhm["det"]
```

Peak statistics are computed per hinted field when a run's table closes, from
the same data as the line plots. Hinted columns and plot dimensions follow the
Start document's `hints` and each stream's descriptor, with the same fallbacks
as the BEC (plan `motors`, then `time`).

## Runs are rendered one at a time

To keep the table output readable, the streamer renders a single run at a time;
updates for later runs wait their turn in arrival order. Two timing parameters
govern the transitions:

- After the Stop document for a run arrives, the streamer waits up to
  `stop_timeout` seconds (default 2) for the remaining table rows, compared
  against `stop["num_events"]`. Streams holding only external array data are
  recognized and not waited for.
- A run that never receives a Stop (for example, a failed run that the writer
  backed up without one) does not block forever: once the next run has been
  waiting `stop_timeout`, the streamer notes the missing Stop and moves on.

By default, only runs started after the streamer connects are shown. Pass
`replay=True` to also render earlier runs still retained by the server's
streaming cache.

## Build your own consumer

`subscribe_to_hinted_streams` is the layer underneath: it subscribes to every
event stream of accepted runs and delivers table updates annotated with the
run's Start document, the stream's descriptor, and the resolved hinted fields —
everything needed to drive a custom table or plot widget.

```python
from bluesky_tiled_plugins.streaming import HintedStreamUpdate, subscribe_to_hinted_streams


def on_update(update: HintedStreamUpdate) -> None:
    print(update.run_uid, update.stream_name, update.fields)
    print(update.data())  # seq_num, time, and the hinted columns


with subscribe_to_hinted_streams(container, on_update):
    ...
```

`update.table` keeps every column `TiledWriter` wrote; `update.data()` narrows
it to `seq_num`, `time`, and the hinted fields. The callback runs on Tiled
worker threads, possibly concurrently — synchronize any shared state, or queue
updates for a GUI thread as `TiledBestEffortStreamer` does. Start documents and
descriptors are cached for recent runs (scaled by `retained_runs`); evicted
streams are refetched transparently.

For selecting specific streams and data keys, raw native updates, and run
filtering, use `subscribe_to_streams` directly — see the
[streaming tutorial](./streaming.md).

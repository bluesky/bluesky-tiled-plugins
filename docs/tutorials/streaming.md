# Subscribe to Live Bluesky Stream Data

`subscribe_to_streams` delivers native Tiled live updates for selected data keys
across one or more named Bluesky event streams. The callback receives a
`BlueskyStreamUpdate`; call `update.data()` to decode its payload.

```python
from bluesky_tiled_plugins import BlueskyStreamUpdate, subscribe_to_streams


def on_update(update: BlueskyStreamUpdate) -> None:
    batch = update.data()
    print(update.run_uid, update.stream_name, update.data_keys, update.sequence)
    print(batch)


with subscribe_to_streams(
    container,
    on_update,
    streams={"baseline": ("x", "y"), "primary": "temperature"},
):
    # Continue producing or consuming data while the subscription is active.
    ...
```

`container` is the direct parent `Container` of the `BlueskyRun` nodes. Leaving
the `with` block disconnects every subscription created for that container,
runs, event streams, and selected data nodes. Call `disconnect()` directly when
a context manager does not fit the application's lifecycle.

## Configure the producer and server

Use clients that address the same parent `Container` for `subscribe_to_streams`
and `TiledWriter`.

```python
from bluesky_tiled_plugins import TiledWriter

writer = TiledWriter(container, batch_size=1)
```

`batch_size=1` makes each Event document available for live delivery. The Tiled
server must have Streaming cache configuration; follow the
[deployment guide](../how-to/deploy.md) to enable it. The default `start=0`
replays only records retained by that cache, not the full historical container.

After a successful Stop, `TiledWriter` closes every writer-owned data node,
event-stream container, and run container from leaves to root. Tiled delivers
each node's pending updates before its normal stream closure, and
`subscribe_to_streams` releases that completed branch while keeping the root
container subscription live for later runs. Producers other than `TiledWriter`
must close their own Tiled streams to receive the same automatic cleanup.

## Select streams and data keys

`streams` is required. Pass `None` to select every future event stream and every
streamable array and table key in accepted runs. This is deliberately explicit
because it may include high-volume data.

```python
with subscribe_to_streams(container, on_update, streams=None):
    ...
```

For a narrower subscription, pass a mapping from each event-stream name to one
key, an iterable of keys, or `None`. Omit a stream to ignore it; a mapping value
of `None` selects every streamable key in that named stream.

```python
with subscribe_to_streams(
    container,
    on_update,
    streams={"baseline": None, "primary": ("temperature", "pressure")},
):
    ...
```

Ragged and bytes nodes are not delivered. Supporting either family requires
upstream Tiled development.

## Run metadata and sequence

Updates carry `run_uid`, not a Start-document field. Retrieve a deep, mutable,
callback-owned copy of persisted metadata from the direct-parent container:

```python
run_metadata = container[update.run_uid].metadata_copy()[0]
start_document = run_metadata.get("start", {})
```

`update.sequence` is Tiled's native positive streaming sequence number for the
node. Use it to correlate delivery and detect duplicates or gaps for one node.
Selected arrays and table columns can be different nodes, so their sequences are
independent.

## Native update batches

For selected scalar columns stored together in one Tiled table, `update.data()`
returns one decoded native table batch with just those columns.

Selected arrays and columns stored in separate tables arrive as independent
callbacks. The API does not synthesize array updates from table columns, buffer
or join updates across nodes, or invent row-alignment and missing-value
behavior.

Updates from different data nodes or event streams have no global ordering, and
Tiled may invoke the shared callback concurrently. Synchronize any mutable state
owned by that callback.

## Filter runs

`subscribe_to_streams` evaluates three optional filters before opening any run,
event-stream, or data-node WebSocket. `metadata_filter` receives the raw
run-creation update's Start document, `required_specs` requires every named
spec, and `run_filter` receives the complete raw `LiveChildCreated` update.

```python
from tiled.client.stream import LiveChildCreated


def accepts_run(update: LiveChildCreated) -> bool:
    return update.key not in ignored_run_uids


subscription = subscribe_to_streams(
    container,
    on_update,
    streams={"baseline": "x", "primary": None},
    metadata_filter=lambda start: start.get("proposal") == "calibration",
    required_specs=("XAS_Calib", "Calibration"),
    run_filter=accepts_run,
)
```

The filters use short-circuiting AND semantics in `metadata_filter`,
`required_specs`, then `run_filter` order. `None` means no condition. A false
result skips that run entirely.

## Readiness and lifetime

The root container WebSocket is connected before `subscribe_to_streams` returns.
Queue Server code can establish the subscription before it submits its first
plan; future run descendants cannot exist until `TiledWriter` creates their
nodes.

When a producer closes a Tiled stream, the manager releases that completed
descendant after its callbacks drain. Closure means no more WebSocket updates;
it does not guarantee that a concurrent REST read can immediately observe every
write.

# Subscribe to Live Bluesky Stream Data

`subscribe_to_stream` delivers physical Tiled Streaming updates for selected
data keys in one named Bluesky event stream. The callback receives a
`BlueskyStreamUpdate`; call `update.data()` to decode its payload.

```python
from bluesky_tiled_plugins import subscribe_to_stream


def on_update(update):
    batch = update.data()
    print(update.run_uid, update.stream_name, update.data_keys, update.sequence)
    print(update.start_document)
    print(batch)


with subscribe_to_stream(tiled_client, "baseline", ("x", "y"), on_update):
    # Continue producing or consuming data while the subscription is active.
    ...
```

Leaving the `with` block disconnects every subscription created for the catalog,
runs, event streams, and selected data nodes. Call `disconnect()` directly when
a context manager does not fit the application's lifecycle.

## Configure the producer and server

Use clients that address the same direct-parent catalog for
`subscribe_to_stream` and `TiledWriter`; they need not be the same Python client
instance. That catalog must be the direct parent of the `BlueskyRun` nodes that
`TiledWriter` creates. The subscription observes those future child-created
updates and does not crawl an arbitrary existing catalog.

```python
from bluesky_tiled_plugins import TiledWriter

writer = TiledWriter(tiled_client, batch_size=1)
```

`batch_size=1` makes each Event document available for live delivery. The Tiled
server must have Streaming cache configuration; follow the
[deployment guide](../how-to/deploy.md) to enable it. The default `start=0`
replays only records retained by that cache, not the full historical catalog.

After a successful Stop, `TiledWriter` closes every writer-owned data node,
event-stream container, and run container from leaves to root. Tiled delivers
each node's pending updates before its normal stream closure, and
`subscribe_to_stream` releases that completed branch while keeping the catalog
subscription live for later runs. Other producers must close their own Tiled
streams to receive the same automatic cleanup.

## Run context and sequence

`update.start_document` is a recursively immutable snapshot of the matching
run's stored `metadata["start"]`, captured once when the run is discovered. It
is an empty immutable mapping when that metadata has no start document. This is
not the untouched inbound RunStart document: `TiledWriter` removes its
`tiled_access_tags` and `tiled_specs` control keys and truncates JSON-overflow
numbers before persisting the metadata.

`update.sequence` is Tiled's native positive streaming sequence number for the
physical node that emitted `update.update`; it is equal to
`update.update.sequence`. Use it to correlate delivery and detect duplicates or
gaps for one node. Selected arrays and table columns can be different physical
nodes, so their sequences are independent. Their native update fields remain on
`update.update`: table updates expose `partition` and `append`, while array
updates expose `offset` and `block` or `patch`; the API does not invent a common
index field.

## Physical update batches

For selected scalar columns stored together in one Tiled table, `update.data()`
returns Tiled's decoded table type with just those columns. With Tiled's
standard table decoder, that is a pandas DataFrame. Selected arrays and columns
stored in separate tables arrive as independent callbacks. The API does not
buffer or join updates across array nodes or tables, so it does not invent row
alignment or missing-value behavior.

## Filter runs

Pass `run_filter` to select runs before any run, event-stream, or data-node
WebSocket is opened. It receives Tiled's raw `LiveChildCreated` update after the
subscription confirms the child has the `BlueskyRun` spec, so it can inspect the
persisted start metadata and custom specs without another catalog request.

```python
from tiled.client.stream import LiveChildCreated


def accepts_run(update: LiveChildCreated) -> bool:
    return (
        update.metadata["start"].get("proposal") == "calibration"
        and any(spec.name == "XAS_Calib" for spec in update.specs)
    )


subscription = subscribe_to_stream(
    tiled_client,
    "baseline",
    "x",
    on_update,
    run_filter=accepts_run,
)
```

The raw update also exposes its key, data sources, and `child()` helper. A false
result skips that run entirely. If the predicate raises, the manager logs the
run UID and child URI and skips that run; the root manager continues serving
later matching runs.

## Readiness and lifetime

The root catalog WebSocket is connected before `subscribe_to_stream` returns.
Queue Server code can establish the subscription before it submits its first
plan; future run descendants cannot exist until `TiledWriter` creates their
nodes.

When a producer closes a Tiled stream, the manager releases that completed
descendant after its callbacks drain. `TiledWriter` does this after successful
finalization; a producer that leaves its streams open retains them until
`disconnect()` is called. `disconnect()` remains the explicit, idempotent, and
blocking manager-wide teardown operation; call it from subscription-owner code,
not from a manager callback.

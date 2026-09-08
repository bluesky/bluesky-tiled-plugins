# Subscribe to Live Bluesky Stream Data

`subscribe_to_stream` delivers physical Tiled Streaming updates for selected data
keys in one named Bluesky event stream. The callback receives a
`BlueskyStreamUpdate`; call `update.data()` to decode its payload.

```python
from bluesky_tiled_plugins import subscribe_to_stream


def on_update(update):
    batch = update.data()
    print(update.run_uid, update.stream_name, update.data_keys)
    print(batch)


with subscribe_to_stream(tiled_client, "baseline", ("x", "y"), on_update):
    # Continue producing or consuming data while the subscription is active.
    ...
```

Leaving the `with` block disconnects every subscription created for the catalog,
runs, event streams, and selected data nodes. Call `disconnect()` directly when a
context manager does not fit the application's lifecycle.

## Configure the producer and server

Pass the same Tiled client to `subscribe_to_stream` and `TiledWriter`. That client
must be the direct parent of the `BlueskyRun` nodes that `TiledWriter` creates; the
subscription observes those future child-created updates and does not crawl an
arbitrary existing catalog.

```python
from bluesky_tiled_plugins import TiledWriter

writer = TiledWriter(tiled_client, batch_size=1)
```

`batch_size=1` makes each Event document available for live delivery. The Tiled
server must have Streaming cache configuration; follow the
[deployment guide](../how-to/deploy.md) to enable it. The default `start=0`
replays only records retained by that cache, not the full historical catalog.

## Physical update batches

For selected scalar columns stored together in one Tiled table, `update.data()`
returns Tiled's decoded table type with just those columns. With Tiled's standard
table decoder, that is a pandas DataFrame. Selected arrays and columns stored in
separate tables arrive as independent callbacks. The API does not buffer or join
updates across array nodes or tables, so it does not invent row alignment or
missing-value behavior.

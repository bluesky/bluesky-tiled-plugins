(deploy-tiled-for-bluesky)=

# Deploy Tiled for Bluesky

## Option 1. Embedded

For "first steps", tutorials, and "embedded" deployments, the
`SimpleTiledServer` is a good solution. It runs a Tiled server on a background
thread.

```python
from tiled.server import SimpleTiledServer

tiled_server = SimpleTiledServer()
tiled_client = from_uri(tiled_server.uri)
```

By default, it uses temporary storage. Pass a directory, e.g.
`SimpleTiledServer("my_data/")` to use persistent storage.

Additionally, if the server needs access to any detector-written files, pass
`SimpleTiledServer(readable_storage=["path/to/detector/data"])`

```{note}

The `SimpleTiledService` does not currently support Tiled's streaming Websockets
API, but support is planned in a future release of Tiled.

```

## Option 2.Single-process

Compared to an embedded deployment, this approach isolates the data access tasks
to a separate process. It also provides more flexibility.

### Quickstart

Launch Tiled with temporary storage. Optionally set a deterministic API key (the
default is random each time you start the server).

And, as above, if the server needs access to any detector-written files, pass
the option `-r ...`. (You can pass `-r ...` multiple times to declare multiple
paths.)

```sh
tiled serve catalog --temp  [--api-key secret] [-r path/to/detector/data]
```

### Persistent storage

To save Bluesky data Tiled needs:

- a "catalog" database (e.g. SQLite) for metadata, and
- a "storage" database (e.g. DuckDB) for the scalar data from Event documents
  consolidated in the tabular form.

Launching Tiled with the following command will initialize both databases:

```sh
tiled serve catalog --init ./catalog.db -w duckdb://./storage.db [--api-key secret] [-r path/to/detector/data]
```

If you desire to use the same Tiled instance to upload processed or analyzed
data, it is recommended to also provide Tiled with a writable filesystem
location, `-w path/to/uploaded/data`, which would be used to save array data (as
Zarr).

To enable the streaming Websockets capability, additionally pass a Redis
connection string such as `--cache redis://localhost:6379` or
`--cache rediss://username:password@localhost:6380`.

## Option 3. Containerized and Scalable

For horizontally scaled deployments, PostgreSQL is currently recommended for
both the catalog and storage databases. (Use separate databases! But they can
share a PostgreSQL instance.)

At NSLS-II, we deploy Tiled horizontally-scaled in 24 containers load-balanced
behind HAproxy.

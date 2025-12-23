# Saving Bluesky Runs into Tiled

## Install Tiled Server

In addition to `bluesky-tiled-plugins`, this tutorial requires `tiled` and its
server dependencies.

<!-- prettier-ignore-start -->

::::{tab-set}
:sync-group: install

:::{tab-item} pip
:sync: pip

You will need Python 3.10 or later. You can check your version of Python by
typing into a terminal:

```sh
python3 --version
```

It is recommended that you work in an isolated “virtual environment”, so this
installation will not interfere with any existing Python software:

```sh
python3 -m venv ./venv
source ./venv/bin/activate
```

You can now use `pip` to install the library and its dependencies:

```sh
python3 -m pip install bluesky-tiled-plugins "tiled[server]"
```

:::

:::{tab-item} conda
:sync: conda

Create a conda environment.

```sh
conda create -n try-tiled
conda activate try-tiled
```

Install the package.

```sh
conda install -c conda-forge bluesky-tiled-plugins tiled-server
```

:::

:::{tab-item} uv
:sync: uv

Create a project.

```sh
uv init
```

Add `bluesky-tiled-plugins` to it.

```sh
uv add bluesky-tiled-plugins "tiled[server]"
```

:::

:::{tab-item} pixi
:sync: pixi

Create a workspace.

```sh
pixi init
```

Add `bluesky-tiled-plugins` to it.

```sh
pixi add bluesky-tiled-plugins tiled-server
```

:::

::::

<!-- prettier-ignore-end -->

## Complete Example

A minimal simulated example of using `TiledWriter` in a Bluesky plan is shown
below.

```python
from bluesky import RunEngine
import bluesky.plans as bp
from bluesky_tiled_plugins import TiledWriter
from tiled.server import SimpleTiledServer
from tiled.client import from_uri
from ophyd.sim import det
from ophyd.sim import hw

# Initialize the Tiled server and client
save_path = "/path/to/save/detector_data"
tiled_server = SimpleTiledServer(readable_storage=[save_path])
tiled_client = from_uri(tiled_server.uri)

# Initialize the RunEngine and subscribe TiledWriter
RE = RunEngine()
tw = TiledWriter(tiled_client, batch_size=1)
RE.subscribe(tw)

# Run an experiment collecting internal data
(uid,) = RE(bp.count([det], 3))
data = tiled_client[uid]["primary/det"].read()

# Run an experiment collecting external data
(uid,) = RE(bp.count([hw(save_path=save_path).img], 2))
data = tiled_client[uid]["primary/img"].read()
```

## Details

### Run `SimpleTiledServer`

This starts a Tiled server, running on a background thread. This way of running
the server is intended for "first steps" and embedded deployments. See the guide
on [deploying Tiled for Bluesky](#deploy-tiled-for-bluesky) for more details.

```python
# Initialize the Tiled server and client
save_path = "/path/to/save/detector_data"
tiled_server = SimpleTiledServer(readable_storage=[save_path])
```

### Connect client

This connects to the server.

```python
tiled_client = from_uri(tiled_server.uri)
```

````{note}
If running the server in a separate process, container, or host, provide the
appropriate address, i.e.

```python
tiled_client = from_uri("http://...")
```
````

When used with detectors that write data directory to storage (e.g. on local
disk, network file system, or object storage), it is necessary to set the
`readable_storage` parameter. This grants the server permission to serve data at
certain file paths(s).

### Subscribe

This configures the RunEngine to publish all Bluesky documents to the
TiledWriter callback.

```python
# Initialize the RunEngine and subscribe TiledWriter
RE = RunEngine()
tw = TiledWriter(tiled_client, batch_size=1)
RE.subscribe(tw)
```

By default `TiledWriter` caches documents into large batches before writing them
to Tiled. For "live" access to data, set `batch_size=1`.

### Acquire Data and Access It

```python
# Run an experiment collecting only internal (Event) data
(uid,) = RE(bp.count([det], 3))
data = tiled_client[uid]["primary/det"].read()

# Run an experiment collecting external (detector) data
(uid,) = RE(bp.count([hw(save_path=save_path).img], 2))
data = tiled_client[uid]["primary/img"].read()
```

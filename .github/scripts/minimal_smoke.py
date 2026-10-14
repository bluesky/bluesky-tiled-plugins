"""Smoke test for a MINIMAL installation of bluesky-tiled-plugins.

Run with plain python (not pytest) in an environment where only the package and
its declared runtime dependencies are installed -- no extras, no dev/test
dependency groups:

    uv venv && uv pip install . && .venv/bin/python .github/scripts/minimal_smoke.py

It verifies that the declared runtime dependencies are sufficient: every
submodule imports, the client-side writing machinery works, and the optional
ragged dependencies are genuinely absent (so this script will fail by design
if run in a full dev/test environment).
"""

import importlib
import pkgutil
import traceback

import bluesky_tiled_plugins as btp

# Modules that intentionally require tiled[server] and are only imported in
# server deployments (e.g. mounted via the server config).
SERVER_ONLY = {"bluesky_tiled_plugins.routers.validator"}

failures = []
for mod in pkgutil.walk_packages(btp.__path__, btp.__name__ + "."):
    if mod.name in SERVER_ONLY:
        continue
    try:
        importlib.import_module(mod.name)
    except Exception:
        failures.append((mod.name, traceback.format_exc()))

for name, tb in failures:
    print(f"FAILED IMPORT: {name}\n{tb}")
if failures:
    raise SystemExit(f"{len(failures)} module(s) failed to import; see above.")

# The public API is usable.
from bluesky_tiled_plugins import TiledInserter, TiledWriter  # noqa: E402, F401
from bluesky_tiled_plugins.writing.consolidators import (  # noqa: E402
    consolidator_factory,
)
from bluesky_tiled_plugins.writing.tiled_writer import RunNormalizer  # noqa: E402

# The document-processing machinery works without a server.
received = []
normalizer = RunNormalizer()
normalizer.subscribe(lambda name, doc: received.append(name))
normalizer("start", {"uid": "u1", "time": 0.0})
normalizer(
    "descriptor",
    {
        "uid": "d1",
        "run_start": "u1",
        "time": 0.0,
        "name": "primary",
        "data_keys": {"x": {"source": "s", "dtype": "number", "shape": []}},
        "object_keys": {"det": ["x"]},
        "configuration": {},
        "hints": {},
    },
)
normalizer(
    "event",
    {
        "uid": "e1",
        "descriptor": "d1",
        "time": 1.0,
        "seq_num": 1,
        "data": {"x": 1.0},
        "timestamps": {"x": 1.0},
        "filled": {},
    },
)
normalizer(
    "stop",
    {
        "uid": "s1",
        "run_start": "u1",
        "time": 2.0,
        "exit_status": "success",
        "reason": "",
        "num_events": {"primary": 1},
    },
)
assert "start" in received and "stop" in received, received

consolidator = consolidator_factory(
    {
        "data_key": "img",
        "uri": "file:///x",
        "mimetype": "multipart/related;type=image/tiff",
        "parameters": {"chunk_shape": (1, 5, 7), "template": "f_{:06d}.tiff"},
    },
    {
        "data_keys": {
            "img": {"shape": [3, 5, 7], "dtype": "array", "dtype_numpy": "|u1"}
        }
    },
)
consolidator.consume_stream_datum(
    {"indices": {"start": 0, "stop": 1}, "seq_nums": {"start": 1, "stop": 2}}
)
assert consolidator.shape and consolidator.assets
assert consolidator.structure() is not None

# The optional ragged dependencies must NOT be present in a minimal install;
# this guards against them sneaking back in through a transitive dependency
# (install bluesky-tiled-plugins[ragged] to get them).
try:
    import tiled.structures.ragged  # noqa: F401
except ImportError:
    pass
else:
    raise SystemExit(
        "tiled.structures.ragged is importable: the environment is not minimal."
    )

print("Minimal installation smoke test: OK")

import asyncio
import os
import uuid

import pytest
from bluesky.run_engine import RunEngine, TransitionError

import h5py
import copy
import tifffile as tf
import numpy as np
from bluesky_tiled_plugins.exporters import json_seq_exporter
from tiled.server.app import build_app
from tiled.media_type_registration import default_serialization_registry
import tiled.catalog
import tiled.client
from bluesky_tiled_plugins.routers.validator import router as validator_router

rng = np.random.default_rng(12345)


@pytest.fixture(scope="function", params=[False, True])
def RE(request):
    loop = asyncio.new_event_loop()
    loop.set_debug(True)
    RE = RunEngine({}, call_returns_result=request.param, loop=loop)

    def clean_event_loop():
        if RE.state not in ("idle", "panicked"):
            try:
                RE.halt()
            except TransitionError:
                pass
        loop.call_soon_threadsafe(loop.stop)
        RE._th.join()
        loop.close()

    request.addfinalizer(clean_event_loop)
    return RE


@pytest.fixture(scope="module")
def catalog(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("tiled_catalog")
    return tiled.catalog.in_memory(
        writable_storage={
            "filesystem": str(tmp_path),
            "sql": f"duckdb:///{tmp_path}/test.db",
        },
        readable_storage=[str(tmp_path.parent)],
    )


@pytest.fixture(scope="module", params=[{}, {"include_routers": [validator_router]}])
def app(catalog, request):
    serialization_registry = copy.deepcopy(default_serialization_registry)
    serialization_registry.register(
        "BlueskyRun", "application/json-seq", json_seq_exporter
    )

    return build_app(
        catalog, serialization_registry=serialization_registry, **request.param
    )


@pytest.fixture(scope="module")
def context(app):
    with tiled.client.Context.from_app(app) as context:
        yield context


@pytest.fixture(scope="module")
def client(context):
    return tiled.client.from_context(context)


@pytest.fixture(scope="module")
def external_assets_folder(tmp_path_factory):
    """External data files used with the saved documents."""
    # Create a temporary directory
    temp_dir = tmp_path_factory.mktemp("example_files")

    # Create an external hdf5 file
    with h5py.File(temp_dir.joinpath("dataset.h5"), "w") as file:
        grp = file.create_group("entry").create_group("data")
        grp.create_dataset("data_1", data=rng.random(size=(3,), dtype="float64"))
        grp.create_dataset(
            "data_2", data=rng.integers(-10, 10, size=(3, 13, 17)), dtype="<i8"
        )

    # Create a sequence of related hdf5 files to be declared in the same stream resource
    for i in range(3):
        parent_dir = temp_dir.joinpath("multipart_hdf5")
        parent_dir.mkdir(parents=True, exist_ok=True)
        with h5py.File(parent_dir.joinpath(f"dataset_part_{i:06d}.h5"), "w") as file:
            grp = file.create_group("entry").create_group("data")
            grp.create_dataset(
                "data", data=rng.random(size=(1, 13, 17), dtype="float64")
            )

    # Create a second external hdf5 file to be declared in a different stream resource
    with h5py.File(temp_dir.joinpath("dataset_part2.h5"), "w") as file:
        grp = file.create_group("entry").create_group("data")
        grp.create_dataset(
            "data_2", data=rng.integers(-10, 10, size=(5, 13, 17)), dtype="<i8"
        )

    # Create a sequence of tiff files
    (temp_dir / "tiff_files").mkdir(parents=True, exist_ok=True)
    for i in range(3):
        data = rng.integers(0, 255, size=(1, 10, 15), dtype="uint8")
        tf.imwrite(temp_dir.joinpath("tiff_files", f"img_{i:05}.tif"), data)

    # Create an opaque binary blob (single-file bytes stream) and a
    # sequence of blobs (multi-file bytes stream with a filename template).
    temp_dir.joinpath("blob.bin").write_bytes(b"\x00\x01\x02opaque-blob\xff")
    (temp_dir / "blob_files").mkdir(parents=True, exist_ok=True)
    for i in range(3):
        payload = f"payload-{i}".encode() + bytes([i])
        temp_dir.joinpath("blob_files", f"blob_{i:05d}.bin").write_bytes(payload)

    return str(temp_dir.absolute()).replace("\\", "/")


# --------------------------------------------------------------------------- #
# Fixtures for TiledInserter (Mongo-backed CatalogOfBlueskyRuns).
#
# These fixtures spin up a Tiled server with a Mongo-backed catalog adapter
# (`databroker.mongo_normalized.MongoAdapter`) and yield a
# `CatalogOfBlueskyRuns` client that exposes the `post_document` /
# "insert" interface used by `TiledInserter`.
#
# Two variants are parametrized:
#   - `mongomock`: always available, in-process, no service required.
#   - `mongo`:     only available when `TEST_MONGO_URI` is set (e.g. by
#                    CI or by `continuous_integration/scripts/start_mongo.sh`).
# --------------------------------------------------------------------------- #


@pytest.fixture(
    scope="function",
    params=[
        "mongomock",
        pytest.param(
            "mongo",
            marks=pytest.mark.skipif(
                not os.environ.get("TEST_MONGO_URI"),
                reason="Requires a running MongoDB; set TEST_MONGO_URI to enable.",
            ),
        ),
    ],
)
def mongo_catalog_client(request):
    """A `CatalogOfBlueskyRuns` client backed by a Mongo-backed Tiled server.

    Parametrized over an in-process `mongomock` adapter and (optionally) a
    real MongoDB instance pointed at by the `TEST_MONGO_URI` env var.
    """
    pytest.importorskip("databroker")
    from databroker import mongo_normalized

    if request.param == "mongomock":
        pytest.importorskip("mongomock")
        adapter = mongo_normalized.MongoAdapter.from_mongomock()
    else:
        # Use a unique database for each test to avoid cross-test pollution.
        base = os.environ.get("TEST_MONGO_URI").rstrip("/")
        db_name = f"bluesky_tiled_plugins_test_{uuid.uuid4().hex}"
        uri = f"{base}/{db_name}"
        adapter = mongo_normalized.MongoAdapter.from_uri(uri)

        def _drop():
            try:
                adapter._metadatastore_db.client.drop_database(db_name)
            except Exception:
                pass

        request.addfinalizer(_drop)

    app = build_app(adapter)
    context = tiled.client.Context.from_app(app)
    request.addfinalizer(context.__exit__)
    client = tiled.client.from_context(context)
    return client


@pytest.fixture(
    scope="function",
    params=[
        "fakeredis",
        pytest.param(
            "redis",
            marks=pytest.mark.skipif(
                not os.environ.get("TEST_REDIS_URI"),
                reason="Requires Redis; set TEST_REDIS_URI to enable.",
            ),
        ),
    ],
)
def redis_json_dict_store(request):
    """A `RedisJSONDict`, parametrized over an in-memory `fakeredis`
    backend (always available) and a real Redis backend pointed at by
    `TEST_REDIS_URI`.
    """
    redis_json_dict = pytest.importorskip("redis_json_dict")
    prefix = f"bluesky_tiled_plugins_test_{uuid.uuid4().hex}:"

    if request.param == "fakeredis":
        fakeredis = pytest.importorskip("fakeredis")
        return redis_json_dict.RedisJSONDict(fakeredis.FakeRedis(), prefix=prefix)

    redis_pkg = pytest.importorskip("redis")
    redis_client = redis_pkg.Redis.from_url(os.environ["TEST_REDIS_URI"])

    def _cleanup():
        keys = list(redis_client.scan_iter(match=f"{prefix}*"))
        if keys:
            redis_client.delete(*keys)

    request.addfinalizer(_cleanup)
    return redis_json_dict.RedisJSONDict(redis_client, prefix=prefix)

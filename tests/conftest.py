import asyncio

import pytest
from bluesky.run_engine import RunEngine, TransitionError

import h5py
import tifffile as tf
import numpy as np

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
    tiled_catalog = pytest.importorskip("tiled.catalog")
    tmp_path = tmp_path_factory.mktemp("tiled_catalog")
    return tiled_catalog.in_memory(
        writable_storage={
            "filesystem": str(tmp_path),
            "sql": f"duckdb:///{tmp_path}/test.db",
        },
        readable_storage=[str(tmp_path.parent)],
    )


@pytest.fixture(scope="module")
def app(catalog):
    tsa = pytest.importorskip("tiled.server.app")
    return tsa.build_app(catalog)


@pytest.fixture(scope="module")
def context(app):
    tc = pytest.importorskip("tiled.client")
    with tc.Context.from_app(app) as context:
        yield context


@pytest.fixture(scope="module")
def client(context):
    tc = pytest.importorskip("tiled.client")
    return tc.from_context(context)


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

    return str(temp_dir.absolute()).replace("\\", "/")

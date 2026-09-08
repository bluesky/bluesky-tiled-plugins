import queue
import uuid

import h5py
import numpy as np
import pytest
import pandas as pd
import tiled.catalog
from tiled.client import Context, from_context
from tiled.server.app import build_app

from bluesky_tiled_plugins import (
    BlueskyStreamUpdate,
    TiledWriter,
    subscribe_to_stream,
)


@pytest.fixture
def streaming_client(tmp_path):
    catalog = tiled.catalog.in_memory(
        writable_storage={
            "filesystem": str(tmp_path),
            "sql": f"duckdb:///{tmp_path}/test.db",
        },
        readable_storage=[str(tmp_path.parent)],
        cache_config={"uri": "memory://", "data_ttl": 60, "seq_ttl": 60},
    )
    with Context.from_app(build_app(catalog)) as context:
        yield from_context(context)


def _start_document(run_uid: str) -> dict:
    return {"uid": run_uid, "time": 0.0}


def _descriptor_document(
    run_uid: str,
    descriptor_uid: str,
    name: str,
    data_keys: dict,
) -> dict:
    return {
        "uid": descriptor_uid,
        "run_start": run_uid,
        "time": 1.0,
        "name": name,
        "data_keys": data_keys,
        "object_keys": {"det": list(data_keys)},
    }


def _scalar_data_keys(*keys: str) -> dict:
    return {
        key: {
            "source": "sim",
            "dtype": "number",
            "shape": [],
            "object_name": "det",
        }
        for key in keys
    }


def _event_document(descriptor_uid: str, seq_num: int, data: dict) -> dict:
    return {
        "uid": uuid.uuid4().hex,
        "descriptor": descriptor_uid,
        "time": 2.0 + seq_num,
        "seq_num": seq_num,
        "data": data,
        "timestamps": {key: 2.0 + seq_num for key in data},
        "filled": {},
    }


def _stop_document(run_uid: str, num_events: dict[str, int]) -> dict:
    return {
        "uid": uuid.uuid4().hex,
        "run_start": run_uid,
        "time": 10.0,
        "exit_status": "success",
        "num_events": num_events,
    }


def test_subscribe_to_stream_filters_table_columns_and_disconnects(streaming_client):
    run_uid = uuid.uuid4().hex
    baseline_descriptor_uid = uuid.uuid4().hex
    primary_descriptor_uid = uuid.uuid4().hex
    updates: queue.Queue[BlueskyStreamUpdate] = queue.Queue()
    writer = TiledWriter(streaming_client, batch_size=1)
    subscription = subscribe_to_stream(
        streaming_client, "baseline", ("x", "y"), updates.put
    )

    try:
        writer("start", _start_document(run_uid))
        writer(
            "descriptor",
            _descriptor_document(
                run_uid,
                baseline_descriptor_uid,
                "baseline",
                _scalar_data_keys("x", "y", "z"),
            ),
        )
        writer(
            "event",
            _event_document(
                baseline_descriptor_uid,
                1,
                {"x": 1.0, "y": 2.0, "z": 3.0},
            ),
        )

        update = updates.get(timeout=5)
        assert update.run_uid == run_uid
        assert update.stream_name == "baseline"
        assert update.data_keys == ("x", "y")
        table = update.data()
        assert isinstance(table, pd.DataFrame)
        assert list(table.columns) == ["x", "y"]
        assert table.to_dict(orient="list") == {"x": [1.0], "y": [2.0]}
        writer(
            "descriptor",
            _descriptor_document(
                run_uid,
                primary_descriptor_uid,
                "primary",
                _scalar_data_keys("x", "y", "z"),
            ),
        )
        writer(
            "event",
            _event_document(
                primary_descriptor_uid,
                1,
                {"x": 4.0, "y": 5.0, "z": 6.0},
            ),
        )
        with pytest.raises(queue.Empty):
            updates.get(timeout=1)

        subscription.disconnect()
        assert subscription.closed
        subscription.disconnect()
        writer(
            "event",
            _event_document(
                baseline_descriptor_uid,
                2,
                {"x": 7.0, "y": 8.0, "z": 9.0},
            ),
        )
        with pytest.raises(queue.Empty):
            updates.get(timeout=1)
    finally:
        subscription.disconnect()
        writer("stop", _stop_document(run_uid, {"baseline": 2, "primary": 1}))


def test_subscribe_to_stream_delivers_selected_array(streaming_client):
    run_uid = uuid.uuid4().hex
    descriptor_uid = uuid.uuid4().hex
    updates: queue.Queue[BlueskyStreamUpdate] = queue.Queue()
    writer = TiledWriter(streaming_client, batch_size=1, max_array_size=0)

    with subscribe_to_stream(streaming_client, "baseline", "image", updates.put):
        writer("start", _start_document(run_uid))
        writer(
            "descriptor",
            _descriptor_document(
                run_uid,
                descriptor_uid,
                "baseline",
                {
                    "image": {
                        "source": "sim",
                        "dtype": "array",
                        "dtype_numpy": "<i8",
                        "shape": [2],
                        "object_name": "det",
                    }
                },
            ),
        )
        writer("event", _event_document(descriptor_uid, 1, {"image": [1, 2]}))

        update = updates.get(timeout=5)
        assert update.run_uid == run_uid
        assert update.stream_name == "baseline"
        assert update.data_keys == ("image",)
        np.testing.assert_array_equal(update.data(), [[1, 2]])

    writer("stop", _stop_document(run_uid, {"baseline": 1}))


def test_subscribe_to_stream_delivers_stream_datum_without_events(
    streaming_client, tmp_path
):
    expected = np.array([[1, 2]], dtype="<i8")
    data_path = tmp_path / "stream.h5"
    with h5py.File(data_path, "w") as file:
        file.create_group("entry").create_group("data").create_dataset(
            "image", data=expected
        )

    run_uid = uuid.uuid4().hex
    descriptor_uid = uuid.uuid4().hex
    resource_uid = uuid.uuid4().hex
    updates: queue.Queue[BlueskyStreamUpdate] = queue.Queue()
    writer = TiledWriter(streaming_client, batch_size=1)

    with subscribe_to_stream(streaming_client, "baseline", "image", updates.put):
        writer("start", _start_document(run_uid))
        writer(
            "descriptor",
            _descriptor_document(
                run_uid,
                descriptor_uid,
                "baseline",
                {
                    "image": {
                        "source": "file",
                        "dtype": "array",
                        "dtype_numpy": "<i8",
                        "shape": [2],
                        "dims": ["time", "dim_1"],
                        "external": "STREAM:",
                        "object_name": "det",
                    }
                },
            ),
        )
        writer(
            "stream_resource",
            {
                "uid": resource_uid,
                "data_key": "image",
                "uri": f"file://localhost{data_path}",
                "mimetype": "application/x-hdf5",
                "parameters": {
                    "dataset": "/entry/data/image",
                    "chunk_shape": [1, 2],
                },
                "run_start": run_uid,
            },
        )
        writer(
            "stream_datum",
            {
                "uid": f"{resource_uid}/0",
                "stream_resource": resource_uid,
                "descriptor": descriptor_uid,
                "indices": {"start": 0, "stop": 1},
                "seq_nums": {"start": 1, "stop": 2},
            },
        )

        update = updates.get(timeout=5)
        assert update.run_uid == run_uid
        assert update.stream_name == "baseline"
        assert update.data_keys == ("image",)
        np.testing.assert_array_equal(update.data(), expected)

    writer("stop", _stop_document(run_uid, {"baseline": 0}))


def test_subscribe_to_stream_rejects_empty_selection(streaming_client):
    with pytest.raises(ValueError, match="At least one data key"):
        subscribe_to_stream(streaming_client, "baseline", (), lambda update: None)

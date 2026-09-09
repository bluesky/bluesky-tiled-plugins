import logging
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


def _start_document(
    run_uid: str,
    *,
    metadata: dict | None = None,
    tiled_specs: list | None = None,
) -> dict:
    document = {"uid": run_uid, "time": 0.0}
    if metadata is not None:
        document.update(metadata)
    if tiled_specs is not None:
        document["tiled_specs"] = tiled_specs
    return document


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


def _subscribe_until_closed(node, closed, name, attach=None):
    subscription = node.subscribe()
    if attach is not None:
        attach(subscription)

    def on_closed(_subscription):
        closed.put(name)

    subscription.stream_closed.add_callback(on_closed)
    subscription.start_in_thread(0)
    return subscription, on_closed


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
        start_document = _start_document(
            run_uid,
            metadata={"sample": {"name": "sample", "tags": ["calibration"]}},
            tiled_specs=[{"name": "XAS_Calib", "version": "1.0"}],
        )
        writer("start", start_document)
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
        assert update.start_document == {
            "uid": run_uid,
            "time": 0.0,
            "sample": {"name": "sample", "tags": ("calibration",)},
        }
        assert "tiled_specs" not in update.start_document
        with pytest.raises(TypeError):
            update.start_document["extra"] = "value"
        with pytest.raises(TypeError):
            update.start_document["sample"]["name"] = "other"
        with pytest.raises(AttributeError):
            update.start_document["sample"]["tags"].append("other")
        assert update.sequence == update.update.sequence
        assert update.sequence > 0
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


def test_tiled_writer_stop_closes_run_stream_and_table_subscriptions(
    streaming_client,
):
    run_uid = uuid.uuid4().hex
    descriptor_uid = uuid.uuid4().hex
    data_updates = queue.Queue()
    closed: queue.Queue[str] = queue.Queue()
    writer = TiledWriter(streaming_client, batch_size=1)
    subscriptions = []

    writer("start", _start_document(run_uid))
    writer(
        "descriptor",
        _descriptor_document(
            run_uid,
            descriptor_uid,
            "baseline",
            _scalar_data_keys("x"),
        ),
    )
    writer("event", _event_document(descriptor_uid, 1, {"x": 1.0}))

    def on_data(update):
        data_updates.put(update)

    try:
        run_subscription, run_closed = _subscribe_until_closed(
            streaming_client[run_uid], closed, "run"
        )
        subscriptions.append(run_subscription)
        stream_subscription, stream_closed = _subscribe_until_closed(
            streaming_client[run_uid]["baseline"], closed, "stream"
        )
        subscriptions.append(stream_subscription)
        table_subscription, table_closed = _subscribe_until_closed(
            streaming_client[run_uid]["baseline"].base["internal"],
            closed,
            "table",
            lambda subscription: subscription.new_data.add_callback(on_data),
        )
        subscriptions.append(table_subscription)

        assert data_updates.get(timeout=5).sequence > 0
        writer("stop", _stop_document(run_uid, {"baseline": 1}))
        assert {closed.get(timeout=5) for _ in range(3)} == {
            "run",
            "stream",
            "table",
        }
    finally:
        for subscription in reversed(subscriptions):
            subscription.disconnect()


def test_subscribe_to_stream_filters_runs(streaming_client):
    accepted_run_uid = uuid.uuid4().hex
    accepted_descriptor_uid = uuid.uuid4().hex
    rejected_run_uid = uuid.uuid4().hex
    rejected_descriptor_uid = uuid.uuid4().hex
    updates: queue.Queue[BlueskyStreamUpdate] = queue.Queue()
    accepted_writer = TiledWriter(streaming_client, batch_size=1)
    rejected_writer = TiledWriter(streaming_client, batch_size=1)

    def run_filter(update):
        return update.metadata["start"]["route"]["accepted"] and any(
            spec.name == "AcceptedRun" for spec in update.specs
        )

    with subscribe_to_stream(
        streaming_client,
        "baseline",
        "x",
        updates.put,
        run_filter=run_filter,
    ):
        accepted_writer(
            "start",
            _start_document(
                accepted_run_uid,
                metadata={"route": {"accepted": True}},
                tiled_specs=[{"name": "AcceptedRun", "version": "1.0"}],
            ),
        )
        accepted_writer(
            "descriptor",
            _descriptor_document(
                accepted_run_uid,
                accepted_descriptor_uid,
                "baseline",
                _scalar_data_keys("x"),
            ),
        )
        accepted_writer(
            "event", _event_document(accepted_descriptor_uid, 1, {"x": 1.0})
        )
        rejected_writer(
            "start",
            _start_document(
                rejected_run_uid,
                metadata={"route": {"accepted": False}},
                tiled_specs=[{"name": "RejectedRun", "version": "1.0"}],
            ),
        )
        rejected_writer(
            "descriptor",
            _descriptor_document(
                rejected_run_uid,
                rejected_descriptor_uid,
                "baseline",
                _scalar_data_keys("x"),
            ),
        )
        rejected_writer(
            "event", _event_document(rejected_descriptor_uid, 1, {"x": 2.0})
        )

        update = updates.get(timeout=5)
        assert update.run_uid == accepted_run_uid
        assert update.data().to_dict(orient="list") == {"x": [1.0]}
        with pytest.raises(queue.Empty):
            updates.get(timeout=1)

    accepted_writer("stop", _stop_document(accepted_run_uid, {"baseline": 1}))
    rejected_writer("stop", _stop_document(rejected_run_uid, {"baseline": 1}))


def test_subscribe_to_stream_logs_run_filter_failures(streaming_client, caplog):
    rejected_run_uid = uuid.uuid4().hex
    rejected_descriptor_uid = uuid.uuid4().hex
    accepted_run_uid = uuid.uuid4().hex
    accepted_descriptor_uid = uuid.uuid4().hex
    updates: queue.Queue[BlueskyStreamUpdate] = queue.Queue()
    rejected_writer = TiledWriter(streaming_client, batch_size=1)
    accepted_writer = TiledWriter(streaming_client, batch_size=1)

    failed_uri: str | None = None

    def run_filter(update):
        nonlocal failed_uri
        if update.metadata["start"]["route"] == "raise":
            failed_uri = str(update.uri)
            raise RuntimeError("known predicate failure")
        return True

    with caplog.at_level(logging.ERROR, logger="bluesky_tiled_plugins.streaming"):
        with subscribe_to_stream(
            streaming_client,
            "baseline",
            "x",
            updates.put,
            run_filter=run_filter,
        ):
            rejected_writer(
                "start",
                _start_document(rejected_run_uid, metadata={"route": "raise"}),
            )
            rejected_writer(
                "descriptor",
                _descriptor_document(
                    rejected_run_uid,
                    rejected_descriptor_uid,
                    "baseline",
                    _scalar_data_keys("x"),
                ),
            )
            rejected_writer(
                "event", _event_document(rejected_descriptor_uid, 1, {"x": 1.0})
            )
            accepted_writer(
                "start",
                _start_document(accepted_run_uid, metadata={"route": "accept"}),
            )
            accepted_writer(
                "descriptor",
                _descriptor_document(
                    accepted_run_uid,
                    accepted_descriptor_uid,
                    "baseline",
                    _scalar_data_keys("x"),
                ),
            )
            accepted_writer(
                "event", _event_document(accepted_descriptor_uid, 1, {"x": 2.0})
            )

            update = updates.get(timeout=5)
            assert update.run_uid == accepted_run_uid
            assert update.data().to_dict(orient="list") == {"x": [2.0]}
            with pytest.raises(queue.Empty):
                updates.get(timeout=1)

    assert failed_uri is not None
    assert any(
        rejected_run_uid in record.getMessage() and failed_uri in record.getMessage()
        for record in caplog.records
    )
    rejected_writer("stop", _stop_document(rejected_run_uid, {"baseline": 1}))
    accepted_writer("stop", _stop_document(accepted_run_uid, {"baseline": 1}))


def test_subscribe_to_stream_stays_open_after_completed_run_drains(streaming_client):
    first_run_uid = uuid.uuid4().hex
    first_descriptor_uid = uuid.uuid4().hex
    second_run_uid = uuid.uuid4().hex
    second_descriptor_uid = uuid.uuid4().hex
    updates: queue.Queue[BlueskyStreamUpdate] = queue.Queue()
    closed: queue.Queue[str] = queue.Queue()
    writer = TiledWriter(streaming_client, batch_size=1)
    subscription = subscribe_to_stream(streaming_client, "baseline", "x", updates.put)
    drain_subscription = None

    try:
        writer("start", _start_document(first_run_uid))
        writer(
            "descriptor",
            _descriptor_document(
                first_run_uid,
                first_descriptor_uid,
                "baseline",
                _scalar_data_keys("x"),
            ),
        )
        writer("event", _event_document(first_descriptor_uid, 1, {"x": 1.0}))

        first_update = updates.get(timeout=5)
        assert first_update.run_uid == first_run_uid
        drain_subscription, drain_closed = _subscribe_until_closed(
            streaming_client[first_run_uid], closed, "first"
        )
        writer("stop", _stop_document(first_run_uid, {"baseline": 1}))
        assert closed.get(timeout=5) == "first"

        writer("start", _start_document(second_run_uid))
        writer(
            "descriptor",
            _descriptor_document(
                second_run_uid,
                second_descriptor_uid,
                "baseline",
                _scalar_data_keys("x"),
            ),
        )
        writer("event", _event_document(second_descriptor_uid, 1, {"x": 2.0}))

        second_update = updates.get(timeout=5)
        assert second_update.run_uid == second_run_uid
        assert second_update.data().to_dict(orient="list") == {"x": [2.0]}
        assert not subscription.closed
    finally:
        if drain_subscription is not None:
            drain_subscription.disconnect()
        subscription.disconnect()

    writer("stop", _stop_document(second_run_uid, {"baseline": 1}))


def test_subscribe_to_stream_delivers_selected_array(streaming_client):
    run_uid = uuid.uuid4().hex
    descriptor_uid = uuid.uuid4().hex
    updates: queue.Queue[BlueskyStreamUpdate] = queue.Queue()
    closed: queue.Queue[str] = queue.Queue()
    writer = TiledWriter(streaming_client, batch_size=1, max_array_size=0)
    data_subscription = None

    try:
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

            data_subscription, data_closed = _subscribe_until_closed(
                streaming_client[run_uid]["baseline"].base["image"], closed, "array"
            )
            writer("stop", _stop_document(run_uid, {"baseline": 1}))
            assert closed.get(timeout=5) == "array"
    finally:
        if data_subscription is not None:
            data_subscription.disconnect()


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
    closed: queue.Queue[str] = queue.Queue()
    writer = TiledWriter(streaming_client, batch_size=1)
    data_subscription = None

    try:
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

            data_subscription, data_closed = _subscribe_until_closed(
                streaming_client[run_uid]["baseline"].base["image"], closed, "external"
            )
            writer("stop", _stop_document(run_uid, {"baseline": 0}))
            assert closed.get(timeout=5) == "external"
    finally:
        if data_subscription is not None:
            data_subscription.disconnect()


def test_subscribe_to_stream_rejects_empty_selection(streaming_client):
    with pytest.raises(ValueError, match="At least one data key"):
        subscribe_to_stream(streaming_client, "baseline", (), lambda update: None)

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
from tiled.client.stream import LiveChildCreated

from bluesky_tiled_plugins import (
    BlueskyStreamUpdate,
    TiledWriter,
    subscribe_to_streams,
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


def test_subscribe_to_streams_filters_table_columns_and_disconnects(streaming_client):
    run_uid = uuid.uuid4().hex
    baseline_descriptor_uid = uuid.uuid4().hex
    primary_descriptor_uid = uuid.uuid4().hex
    updates: queue.Queue[tuple[BlueskyStreamUpdate, dict]] = queue.Queue()
    writer = TiledWriter(streaming_client, batch_size=1)

    def on_update(update: BlueskyStreamUpdate) -> None:
        start_document = (
            streaming_client[update.run_uid].metadata_copy()[0].get("start", {})
        )
        updates.put((update, start_document))

    subscription = subscribe_to_streams(
        streaming_client, on_update, streams={"baseline": ("x", "y")}
    )

    try:
        writer(
            "start",
            _start_document(
                run_uid,
                metadata={"sample": {"name": "sample", "tags": ["calibration"]}},
                tiled_specs=[{"name": "XAS_Calib", "version": "1.0"}],
            ),
        )
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

        update, start_document = updates.get(timeout=5)
        assert update.run_uid == run_uid
        assert update.stream_name == "baseline"
        assert update.data_keys == ("x", "y")
        table = update.data()
        assert isinstance(table, pd.DataFrame)
        assert list(table.columns) == ["x", "y"]
        assert table.to_dict(orient="list") == {"x": [1.0], "y": [2.0]}
        assert start_document == {
            "uid": run_uid,
            "time": 0.0,
            "sample": {"name": "sample", "tags": ["calibration"]},
        }
        assert "tiled_specs" not in start_document
        start_document["sample"]["tags"].append("local mutation")
        assert update.sequence == update.update.sequence
        assert update.sequence > 0

        writer(
            "event",
            _event_document(
                baseline_descriptor_uid,
                2,
                {"x": 7.0, "y": 8.0, "z": 9.0},
            ),
        )
        second_update, second_start_document = updates.get(timeout=5)
        assert second_update.data().to_dict(orient="list") == {
            "x": [7.0],
            "y": [8.0],
        }
        assert second_start_document["sample"]["tags"] == ["calibration"]

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
                3,
                {"x": 10.0, "y": 11.0, "z": 12.0},
            ),
        )
        with pytest.raises(queue.Empty):
            updates.get(timeout=1)
    finally:
        subscription.disconnect()
        writer("stop", _stop_document(run_uid, {"baseline": 3, "primary": 1}))


def test_subscribe_to_streams_delivers_multiple_streams(streaming_client):
    run_uid = uuid.uuid4().hex
    baseline_descriptor_uid = uuid.uuid4().hex
    primary_descriptor_uid = uuid.uuid4().hex
    diagnostics_descriptor_uid = uuid.uuid4().hex
    updates: queue.Queue[BlueskyStreamUpdate] = queue.Queue()
    writer = TiledWriter(streaming_client, batch_size=1)

    with subscribe_to_streams(
        streaming_client,
        updates.put,
        streams={"baseline": ("x", "y"), "primary": None},
    ):
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
        baseline_update = updates.get(timeout=5)
        assert baseline_update.stream_name == "baseline"
        assert baseline_update.data_keys == ("x", "y")
        assert baseline_update.data().to_dict(orient="list") == {
            "x": [1.0],
            "y": [2.0],
        }

        writer(
            "descriptor",
            _descriptor_document(
                run_uid,
                primary_descriptor_uid,
                "primary",
                _scalar_data_keys("signal"),
            ),
        )
        writer(
            "event",
            _event_document(primary_descriptor_uid, 1, {"signal": 4.0}),
        )
        primary_update = updates.get(timeout=5)
        assert primary_update.stream_name == "primary"
        assert primary_update.data_keys == ("signal",)
        assert primary_update.data().to_dict(orient="list") == {"signal": [4.0]}

        writer(
            "descriptor",
            _descriptor_document(
                run_uid,
                diagnostics_descriptor_uid,
                "diagnostics",
                _scalar_data_keys("x"),
            ),
        )
        writer(
            "event",
            _event_document(diagnostics_descriptor_uid, 1, {"x": 5.0}),
        )
        with pytest.raises(queue.Empty):
            updates.get(timeout=1)

    writer(
        "stop",
        _stop_document(run_uid, {"baseline": 1, "primary": 1, "diagnostics": 1}),
    )


def test_subscribe_to_streams_selects_every_stream_when_none(streaming_client):
    run_uid = uuid.uuid4().hex
    baseline_descriptor_uid = uuid.uuid4().hex
    primary_descriptor_uid = uuid.uuid4().hex
    updates: queue.Queue[BlueskyStreamUpdate] = queue.Queue()
    writer = TiledWriter(streaming_client, batch_size=1)

    with subscribe_to_streams(streaming_client, updates.put, streams=None):
        writer("start", _start_document(run_uid))
        writer(
            "descriptor",
            _descriptor_document(
                run_uid,
                baseline_descriptor_uid,
                "baseline",
                _scalar_data_keys("x", "y"),
            ),
        )
        writer(
            "event",
            _event_document(baseline_descriptor_uid, 1, {"x": 1.0, "y": 2.0}),
        )
        baseline_update = updates.get(timeout=5)
        assert baseline_update.stream_name == "baseline"
        assert baseline_update.data_keys == ("x", "y")
        assert baseline_update.data().to_dict(orient="list") == {
            "x": [1.0],
            "y": [2.0],
        }

        writer(
            "descriptor",
            _descriptor_document(
                run_uid,
                primary_descriptor_uid,
                "primary",
                _scalar_data_keys("signal"),
            ),
        )
        writer(
            "event",
            _event_document(primary_descriptor_uid, 1, {"signal": 3.0}),
        )
        primary_update = updates.get(timeout=5)
        assert primary_update.stream_name == "primary"
        assert primary_update.data_keys == ("signal",)
        assert primary_update.data().to_dict(orient="list") == {"signal": [3.0]}

    writer("stop", _stop_document(run_uid, {"baseline": 1, "primary": 1}))


def test_subscribe_to_streams_selects_all_native_batches(streaming_client):
    run_uid = uuid.uuid4().hex
    descriptor_uid = uuid.uuid4().hex
    updates: queue.Queue[BlueskyStreamUpdate] = queue.Queue()
    writer = TiledWriter(streaming_client, batch_size=2, max_array_size=0)
    data_keys = _scalar_data_keys("x", "y", "z")
    data_keys["image"] = {
        "source": "sim",
        "dtype": "array",
        "dtype_numpy": "<i8",
        "shape": [2],
        "object_name": "det",
    }

    with subscribe_to_streams(
        streaming_client, updates.put, streams={"baseline": None}
    ):
        writer("start", _start_document(run_uid))
        writer(
            "descriptor",
            _descriptor_document(
                run_uid,
                descriptor_uid,
                "baseline",
                data_keys,
            ),
        )
        writer(
            "event",
            _event_document(
                descriptor_uid,
                1,
                {"x": 1.0, "y": 2.0, "z": 3.0, "image": [10, 11]},
            ),
        )
        writer(
            "event",
            _event_document(
                descriptor_uid,
                2,
                {"x": 4.0, "y": 5.0, "z": 6.0, "image": [12, 13]},
            ),
        )

        delivered = [updates.get(timeout=5), updates.get(timeout=5)]
        updates_by_keys = {update.data_keys: update for update in delivered}
        assert set(updates_by_keys) == {("x", "y", "z"), ("image",)}

        table = updates_by_keys[("x", "y", "z")].data()
        assert isinstance(table, pd.DataFrame)
        assert tuple(table.columns) == ("x", "y", "z")
        assert table.to_dict(orient="list") == {
            "x": [1.0, 4.0],
            "y": [2.0, 5.0],
            "z": [3.0, 6.0],
        }
        np.testing.assert_array_equal(
            updates_by_keys[("image",)].data(), [[10, 11], [12, 13]]
        )

    writer("stop", _stop_document(run_uid, {"baseline": 2}))


def test_subscribe_to_streams_skips_ragged_and_bytes_nodes(streaming_client, tmp_path):
    run_uid = uuid.uuid4().hex
    descriptor_uid = uuid.uuid4().hex
    resource_uid = uuid.uuid4().hex
    updates: queue.Queue[BlueskyStreamUpdate] = queue.Queue()
    closed: queue.Queue[str] = queue.Queue()
    writer = TiledWriter(streaming_client, batch_size=1)
    blob_path = tmp_path / "blob.bin"
    blob_path.write_bytes(b"\x00")
    data_keys = _scalar_data_keys("x")
    data_keys.update(
        {
            "ragged": {
                "source": "sim",
                "dtype": "array",
                "shape": [2, None],
                "object_name": "det",
            },
            "blob": {
                "source": "file",
                "dtype": "array",
                "dtype_numpy": "|u1",
                "shape": [1],
                "external": "STREAM:",
                "object_name": "det",
            },
        }
    )
    drain_subscription = None

    try:
        with subscribe_to_streams(
            streaming_client, updates.put, streams={"baseline": None}
        ):
            writer("start", _start_document(run_uid))
            writer(
                "descriptor",
                _descriptor_document(
                    run_uid,
                    descriptor_uid,
                    "baseline",
                    data_keys,
                ),
            )
            writer(
                "event",
                _event_document(
                    descriptor_uid,
                    1,
                    {"x": 1.0, "ragged": [[1, 2, 3], [4, 5]]},
                ),
            )
            writer(
                "stream_resource",
                {
                    "uid": resource_uid,
                    "data_key": "blob",
                    "uri": blob_path.as_uri(),
                    "mimetype": "application/octet-stream",
                    "parameters": {},
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
            drain_subscription, drain_closed = _subscribe_until_closed(
                streaming_client[run_uid], closed, "run"
            )
            writer("stop", _stop_document(run_uid, {"baseline": 1}))
            assert closed.get(timeout=5) == "run"

            delivered = []
            while True:
                try:
                    delivered.append(updates.get_nowait())
                except queue.Empty:
                    break
            assert len(delivered) == 1
            assert delivered[0].data_keys == ("x",)
            assert delivered[0].data().to_dict(orient="list") == {"x": [1.0]}
    finally:
        if drain_subscription is not None:
            drain_subscription.disconnect()


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


@pytest.mark.parametrize(
    ("metadata", "specs", "expected_delivery", "expected_run_filter"),
    [
        pytest.param(
            {"group": "rejected", "route": "accepted"},
            ["Calibration", "Required"],
            False,
            False,
            id="metadata-mismatch",
        ),
        pytest.param(
            {"group": "accepted", "route": "accepted"},
            ["Calibration"],
            False,
            False,
            id="spec-mismatch",
        ),
        pytest.param(
            {"group": "accepted", "route": "rejected"},
            ["Calibration", "Required"],
            False,
            True,
            id="raw-filter-mismatch",
        ),
        pytest.param(
            {"group": "accepted", "route": "accepted"},
            ["Calibration", "Required"],
            True,
            True,
            id="accepted",
        ),
    ],
)
def test_subscribe_to_streams_combines_filters(
    streaming_client,
    metadata,
    specs,
    expected_delivery,
    expected_run_filter,
):
    run_uid = uuid.uuid4().hex
    descriptor_uid = uuid.uuid4().hex
    updates: queue.Queue[BlueskyStreamUpdate] = queue.Queue()
    metadata_seen: list[str] = []
    run_filter_seen: list[str] = []
    writer = TiledWriter(streaming_client, batch_size=1)

    def metadata_filter(start):
        metadata_seen.append(start["uid"])
        return start["group"] == "accepted"

    def run_filter(update: LiveChildCreated):
        run_filter_seen.append(update.key)
        return update.metadata["start"]["route"] == "accepted"

    with subscribe_to_streams(
        streaming_client,
        updates.put,
        streams={"baseline": "x"},
        metadata_filter=metadata_filter,
        required_specs=("Calibration", "Required"),
        run_filter=run_filter,
    ):
        writer(
            "start",
            _start_document(
                run_uid,
                metadata=metadata,
                tiled_specs=specs,
            ),
        )
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

        if expected_delivery:
            update = updates.get(timeout=5)
            assert update.run_uid == run_uid
            assert update.data().to_dict(orient="list") == {"x": [1.0]}
        else:
            with pytest.raises(queue.Empty):
                updates.get(timeout=1)

    assert metadata_seen == [run_uid]
    assert run_filter_seen == ([run_uid] if expected_run_filter else [])
    writer("stop", _stop_document(run_uid, {"baseline": 1}))


def test_subscribe_to_streams_accepts_single_required_spec(streaming_client):
    run_uid = uuid.uuid4().hex
    descriptor_uid = uuid.uuid4().hex
    updates: queue.Queue[BlueskyStreamUpdate] = queue.Queue()
    writer = TiledWriter(streaming_client, batch_size=1)

    with subscribe_to_streams(
        streaming_client,
        updates.put,
        streams={"baseline": "x"},
        required_specs="Calibration",
    ):
        writer(
            "start",
            _start_document(run_uid, tiled_specs=["Calibration"]),
        )
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

        update = updates.get(timeout=5)
        assert update.run_uid == run_uid

    writer("stop", _stop_document(run_uid, {"baseline": 1}))


def test_subscribe_to_streams_logs_filtered_run_failures(streaming_client, caplog):
    rejected_run_uid = uuid.uuid4().hex
    rejected_descriptor_uid = uuid.uuid4().hex
    accepted_run_uid = uuid.uuid4().hex
    accepted_descriptor_uid = uuid.uuid4().hex
    updates: queue.Queue[BlueskyStreamUpdate] = queue.Queue()
    rejected_writer = TiledWriter(streaming_client, batch_size=1)
    accepted_writer = TiledWriter(streaming_client, batch_size=1)

    failed_uri: str | None = None

    def run_filter(run: LiveChildCreated):
        nonlocal failed_uri
        if run.metadata["start"]["route"] == "raise":
            failed_uri = str(run.uri)
            raise RuntimeError("known predicate failure")
        return True

    with caplog.at_level(logging.ERROR, logger="bluesky_tiled_plugins.streaming"):
        with subscribe_to_streams(
            streaming_client,
            updates.put,
            streams={"baseline": "x"},
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
    failure_message = next(
        record.getMessage()
        for record in caplog.records
        if rejected_run_uid in record.getMessage()
    )
    assert f"Tiled run {rejected_run_uid} at " in failure_message
    assert failed_uri in failure_message
    rejected_writer("stop", _stop_document(rejected_run_uid, {"baseline": 1}))
    accepted_writer("stop", _stop_document(accepted_run_uid, {"baseline": 1}))


def test_subscribe_to_streams_stays_open_after_completed_run_drains(streaming_client):
    first_run_uid = uuid.uuid4().hex
    first_descriptor_uid = uuid.uuid4().hex
    second_run_uid = uuid.uuid4().hex
    second_descriptor_uid = uuid.uuid4().hex
    updates: queue.Queue[BlueskyStreamUpdate] = queue.Queue()
    closed: queue.Queue[str] = queue.Queue()
    writer = TiledWriter(streaming_client, batch_size=1)
    subscription = subscribe_to_streams(
        streaming_client, updates.put, streams={"baseline": "x"}
    )
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


def test_subscribe_to_streams_delivers_selected_array(streaming_client):
    run_uid = uuid.uuid4().hex
    descriptor_uid = uuid.uuid4().hex
    updates: queue.Queue[BlueskyStreamUpdate] = queue.Queue()
    closed: queue.Queue[str] = queue.Queue()
    writer = TiledWriter(streaming_client, batch_size=1, max_array_size=0)
    data_subscription = None

    try:
        with subscribe_to_streams(
            streaming_client, updates.put, streams={"baseline": "image"}
        ):
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


def test_subscribe_to_streams_delivers_stream_datum_without_events(
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
        with subscribe_to_streams(
            streaming_client, updates.put, streams={"baseline": "image"}
        ):
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
                    "uri": data_path.as_uri(),
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


@pytest.mark.parametrize(
    ("streams", "message"),
    [
        ({}, "At least one stream"),
        ({"baseline": ()}, "At least one data key.*'baseline'"),
    ],
)
def test_subscribe_to_streams_rejects_empty_selection(
    streaming_client, streams, message
):
    with pytest.raises(ValueError, match=message):
        subscribe_to_streams(streaming_client, lambda update: None, streams=streams)

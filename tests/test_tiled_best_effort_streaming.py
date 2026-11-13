import time

import matplotlib
import pandas as pd
import pytest

matplotlib.use("Agg")

from bluesky import RunEngine  # noqa: E402
from bluesky.plans import count, scan  # noqa: E402
from bluesky.preprocessors import SupplementalData  # noqa: E402
from bluesky_tiled_plugins import TiledWriter  # noqa: E402
from ophyd.sim import det, motor, motor1  # noqa: E402
from tiled.client import from_uri  # noqa: E402
from tiled.server import SimpleTiledServer  # noqa: E402

from bluesky_tiled_plugins.streaming import TiledBestEffortStreamer, subscribe_to_hinted_streams  # noqa: E402


@pytest.fixture(scope="module")
def client():
    server = SimpleTiledServer()
    client = from_uri(server.uri)
    yield client
    client.context.close()
    server.close()


@pytest.fixture
def run_engine(client):
    RE = RunEngine()
    RE.subscribe(TiledWriter(client))
    return RE


def _wait_for_finish(bec, uid, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        bec.process_events(timeout=0.1)
        if bec._run is None and not bec._deferred and bec._queue.empty():
            return
    raise TimeoutError(f"Run {uid} was not finished by the callback")


def test_scan_table_plot_and_peaks(client, run_engine):
    lines = []
    with TiledBestEffortStreamer(client, out=lines.append) as bec:
        (uid,) = run_engine(scan([det], motor, -1, 1, 5))
        bec.process_events(timeout=0.5)
        _wait_for_finish(bec, uid)

    text = "\n".join(lines)
    print(text)
    assert f"Persistent Unique Scan ID: '{uid}'" in text
    assert "motor" in text and "det" in text
    data_rows = [line for line in lines if line.startswith("|") and line.split("|")[1].strip().isdigit()]
    assert len(data_rows) == 5
    assert f"generator scan ['{uid[:8]}']" in text
    assert "det" in bec.peaks
    assert bec.peaks["det"]["max"][0] == pytest.approx(0.0)


@pytest.mark.parametrize("retained_runs", [4, 0])
def test_subscribe_to_hinted_streams(client, run_engine, retained_runs):
    updates = []
    stops = []
    subscription = subscribe_to_hinted_streams(
        client,
        updates.append,
        on_run_stop=lambda uid, stop: stops.append(uid),
        retained_runs=retained_runs,
    )
    try:
        (uid,) = run_engine(scan([det], motor, -1, 1, 5))
        deadline = time.monotonic() + 15
        while (
            sum(len(u.table) for u in updates if u.run_uid == uid) < 5 or uid not in stops
        ) and time.monotonic() < deadline:
            time.sleep(0.1)
    finally:
        subscription.disconnect()

    assert uid in stops
    primary = [u for u in updates if u.run_uid == uid and u.stream_name == "primary"]
    assert primary
    assert primary[0].fields == ("motor", "det")
    assert primary[0].start["uid"] == uid
    data = pd.concat([u.data() for u in primary])
    assert list(data.columns) == ["seq_num", "time", "motor", "det"]
    assert sorted(data["seq_num"]) == [1, 2, 3, 4, 5]


def test_baseline_and_time_dimension(client, run_engine):
    sd = SupplementalData(baseline=[motor1])
    run_engine.preprocessors.append(sd)
    lines = []
    with TiledBestEffortStreamer(client, out=lines.append) as bec:
        (uid,) = run_engine(count([det], num=3))
        _wait_for_finish(bec, uid)

    text = "\n".join(lines)
    print(text)
    assert "Start-of-run baseline readings:" in text
    assert "End-of-run baseline readings:" in text
    assert "motor1" in text
    assert f"generator count ['{uid[:8]}']" in text
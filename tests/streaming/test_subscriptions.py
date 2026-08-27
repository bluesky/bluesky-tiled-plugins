from __future__ import annotations

import warnings

# Pre-import websockets modules that trigger DeprecationWarnings so they are
# cached in sys.modules before uvicorn's server thread imports them.
with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    try:
        import uvicorn.protocols.websockets.auto  # noqa: F401
    except (ImportError, Exception):
        pass

import threading
import time
from collections.abc import Generator
from pathlib import Path

import bluesky.plans as bp
import pytest
from bluesky.run_engine import RunEngine
from ophyd.sim import SynGauss, det, motor
from tiled.client import from_uri
from tiled.client.container import Container
from tiled.server.simple import SimpleTiledServer

from bluesky_tiled_plugins import TiledWriter
from bluesky_tiled_plugins.streaming import CatalogSubscription, subscribe_catalog
from bluesky_tiled_plugins.streaming.subscriptions import (
    _matches_metadata_filters,
    _validate_callback,
)

# SimpleTiledServer uses uvicorn which emits DeprecationWarnings during
# startup.  The project-wide filterwarnings=["error"] would kill the server
# thread, so we relax it for this module.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tiled_client(tmp_path: Path) -> Generator[Container, None, None]:
    """Spin up a SimpleTiledServer and yield a connected client."""
    import warnings as _w

    # SimpleTiledServer launches uvicorn in a thread.  The server thread
    # inherits the main thread's warning filters.  pytest sets
    # filterwarnings=["error"], which turns DeprecationWarnings from
    # websockets/uvicorn into exceptions that kill the server thread
    # before it can bind the port.  We temporarily install a filter that
    # the spawned thread will inherit.
    _w.filterwarnings("ignore", category=DeprecationWarning)

    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    server = SimpleTiledServer(tmp_path / "tiled_data")
    client = from_uri(server.uri)
    yield client
    client.context.close()

    #TODO: This is a hack to force uvicorn to exit immediately. Remove once fixed upstream
    # Once a streaming websocket has been opened, uvicorn's graceful shutdown
    # (should_exit) waits forever for the connection to drain, so server.close()
    # -> thread.join() hangs.  Force the uvicorn server to exit immediately.
    try:
        threaded_server = server._cm.gen.gi_frame.f_locals["self"]
        threaded_server.force_exit = True
    except (AttributeError, KeyError, TypeError):
        pass
    server.close()


@pytest.fixture()
def RE() -> RunEngine:
    """A function-scoped RunEngine."""
    return RunEngine({})


# ---------------------------------------------------------------------------
# Unit tests: _validate_callback
# ---------------------------------------------------------------------------


def test_validate_callback_keyword_only():
    def cb(catalog, *, det, motor):
        pass

    _validate_callback(cb, ("det", "motor"))


def test_validate_callback_var_kwargs():
    def cb(catalog, **kw):
        pass

    _validate_callback(cb, ("det", "motor"))


def test_validate_callback_missing_positional():
    def cb(*, det, motor):
        pass

    with pytest.raises(TypeError, match="positional"):
        _validate_callback(cb, ("det", "motor"))


def test_validate_callback_missing_keyword():
    def cb(catalog, *, det):
        pass

    with pytest.raises(TypeError, match="motor"):
        _validate_callback(cb, ("det", "motor"))


def test_validate_callback_positional_or_keyword_params():
    def cb(catalog, det, motor):
        pass

    _validate_callback(cb, ("det", "motor"))


# ---------------------------------------------------------------------------
# Unit tests: _matches_metadata_filters
# ---------------------------------------------------------------------------


def test_matches_metadata_filters_empty():
    assert _matches_metadata_filters({"start": {"plan_name": "count"}}, {})


def test_matches_metadata_filters_matching():
    meta = {"start": {"plan_name": "count", "num": 3}}
    assert _matches_metadata_filters(meta, {"plan_name": "count"})


def test_matches_metadata_filters_non_matching():
    meta = {"start": {"plan_name": "count"}}
    assert not _matches_metadata_filters(meta, {"plan_name": "scan"})


def test_matches_metadata_filters_multiple_all_match():
    meta = {"start": {"plan_name": "count", "num_points": 5}}
    assert _matches_metadata_filters(meta, {"plan_name": "count", "num_points": 5})


def test_matches_metadata_filters_partial_match():
    meta = {"start": {"plan_name": "count", "num_points": 5}}
    assert not _matches_metadata_filters(
        meta, {"plan_name": "count", "num_points": 10}
    )


def test_matches_metadata_filters_missing_start_key():
    assert not _matches_metadata_filters({}, {"plan_name": "count"})


# ---------------------------------------------------------------------------
# Unit tests: subscribe_catalog validation
# ---------------------------------------------------------------------------


def test_subscribe_catalog_rejects_invalid_callback(tiled_client):
    def bad_cb(*, det):
        pass

    with pytest.raises(TypeError, match="positional"):
        subscribe_catalog(tiled_client, "primary", ("det",), bad_cb)


def test_subscribe_catalog_returns_catalog_subscription(tiled_client):
    def cb(catalog, *, det):
        pass

    sub = subscribe_catalog(tiled_client, "primary", ("det",), cb)
    assert isinstance(sub, CatalogSubscription)


# ---------------------------------------------------------------------------
# Integration tests: streaming round-trip
# ---------------------------------------------------------------------------


def test_count_streams_det(tiled_client, RE):
    """Run a count plan and confirm det data is streamed."""
    received = []
    received_event = threading.Event()

    def on_data(catalog, *, det):
        received.append({"det": det})
        received_event.set()

    tw = TiledWriter(tiled_client)
    sub = subscribe_catalog(tiled_client, "primary", ("det",), on_data)
    sub.start_in_thread()
    time.sleep(0.5)

    try:
        RE(bp.count([det], 3), tw)

        assert received_event.wait(timeout=15), (
            f"Timed out waiting for streaming data. Received so far: {received}"
        )
        assert len(received) > 0
        assert "det" in received[0]
        assert len(received[0]["det"]) == 3
    finally:
        sub.stop()


def test_scan_streams_multiple_datasets(tiled_client, RE):
    """Stream det + motor from a scan and verify synchronized callback."""
    received = []
    received_event = threading.Event()

    noisy_det = SynGauss(
        "noisy_det",
        motor,
        "motor",
        center=0,
        Imax=1,
        sigma=1,
        labels={"detectors"},
    )

    def on_data(catalog, *, noisy_det, motor):
        received.append({"noisy_det": noisy_det, "motor": motor})
        received_event.set()

    tw = TiledWriter(tiled_client)
    sub = subscribe_catalog(
        tiled_client, "primary", ("noisy_det", "motor"), on_data, plan_name="scan"
    )
    sub.start_in_thread()
    time.sleep(0.5)

    try:
        RE(bp.scan([noisy_det], motor, -1, 1, 5), tw)

        assert received_event.wait(timeout=15), (
            f"Timed out waiting for streaming data. Received so far: {received}"
        )
        assert len(received) > 0
        assert "noisy_det" in received[0]
        assert "motor" in received[0]
        assert len(received[0]["motor"]) == 5
    finally:
        sub.stop()


def test_metadata_filter_excludes_non_matching(tiled_client, RE):
    """Runs filtered for plan_name='scan' should NOT fire on a count plan."""
    received = []

    def on_data(catalog, *, det):
        received.append(True)

    tw = TiledWriter(tiled_client)
    sub = subscribe_catalog(
        tiled_client, "primary", ("det",), on_data, plan_name="scan"
    )
    sub.start_in_thread()
    time.sleep(0.5)

    try:
        RE(bp.count([det], 2), tw)
        time.sleep(3)
        assert len(received) == 0, (
            f"Callback should not have fired for non-matching plan. Got: {received}"
        )
    finally:
        sub.stop()


def test_context_manager(tiled_client, RE):
    """CatalogSubscription works as a context manager."""
    received = []
    received_event = threading.Event()

    def on_data(catalog, *, det):
        received.append({"det": det})
        received_event.set()

    tw = TiledWriter(tiled_client)

    with subscribe_catalog(
        tiled_client, "primary", ("det",), on_data
    ).start_in_thread():
        RE(bp.count([det], 2), tw)
        assert received_event.wait(timeout=15), "Timed out waiting for data"

    assert len(received) > 0


def test_callback_receives_catalog(tiled_client, RE):
    """Callback receives the catalog as its first positional argument."""
    catalog_ref = []
    received_event = threading.Event()

    def on_data(catalog, *, det):
        catalog_ref.append(catalog)
        received_event.set()

    tw = TiledWriter(tiled_client)
    sub = subscribe_catalog(tiled_client, "primary", ("det",), on_data)
    sub.start_in_thread()
    time.sleep(0.5)

    try:
        RE(bp.count([det], 2), tw)

        assert received_event.wait(timeout=15), "Timed out waiting for data"
        assert catalog_ref[0] is tiled_client
    finally:
        sub.stop()
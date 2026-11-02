"""Tests for the vendored `BufferingWrapper` (see writing/_buffer.py).

Kept in sync with the upstream tests in bluesky (`tests/test_buffering.py`),
minus the bluesky-specific fixtures.
"""

import threading
import time

import pytest

from bluesky_tiled_plugins import BufferingWrapper


class SlowDummyCallback:
    """Simulates a slow (blocking) callback for testing threaded wrappers."""

    def __init__(self, delay=0.1):
        self.delay = delay
        self.called = []

    def __call__(self, name, doc):
        time.sleep(self.delay)
        self.called.append((name, doc))


class CallbackWithException:
    """A callback that raises an exception when called."""

    def __call__(self, name, doc):
        raise RuntimeError("This callback always raises an exception.")


class BlockingCallback:
    """A callback that blocks until released, for exercising flush timeouts."""

    def __init__(self):
        self.release = threading.Event()
        self.called = []

    def __call__(self, name, doc):
        self.release.wait()
        self.called.append((name, doc))


@pytest.fixture
def make_wrapper():
    """Factory for `BufferingWrapper` instances that are shut down on teardown."""
    created: list[BufferingWrapper] = []

    def factory(*args, **kwargs):
        wrapper = BufferingWrapper(*args, **kwargs)
        created.append(wrapper)
        return wrapper

    yield factory

    for wrapper in created:
        wrapper.shutdown()


def test_calls_are_delegated(make_wrapper):
    cb = SlowDummyCallback(delay=0)
    buff_cb = make_wrapper(cb)
    buff_cb("start", {"uid": "abc"})
    assert buff_cb.flush(timeout=5) is True
    assert cb.called == [("start", {"uid": "abc"})]


def test_shutdown_refuses_new_documents(make_wrapper):
    cb = SlowDummyCallback(delay=0)
    buff_cb = make_wrapper(cb)
    buff_cb("one", {})
    buff_cb.shutdown(wait=True)
    with pytest.raises(RuntimeError):
        buff_cb("two", {})
    assert ("one", {}) in cb.called
    assert ("two", {}) not in cb.called


def test_exception_in_callback_does_not_crash_worker(make_wrapper):
    buff_cb = make_wrapper(CallbackWithException())
    buff_cb("event", {})
    assert buff_cb.flush(timeout=5) is True
    assert buff_cb._thread.is_alive()
    buff_cb("event", {})  # still accepting documents


def test_flush_waits_for_processing(make_wrapper):
    """flush() returns only after all enqueued documents are processed."""
    cb = SlowDummyCallback(delay=0.02)
    buff_cb = make_wrapper(cb)

    for i in range(10):
        buff_cb("event", {"seq_num": i})

    assert buff_cb.flush() is True
    assert len(cb.called) == 10  # all documents delivered, in order
    assert [doc["seq_num"] for _, doc in cb.called] == list(range(10))


def test_flush_empty_buffer_returns_immediately(make_wrapper):
    """flush() on an empty buffer is an idempotent no-op, however many times
    it is called, with or without a timeout."""
    buff_cb = make_wrapper(SlowDummyCallback(delay=0))
    for _ in range(3):
        assert buff_cb.flush(timeout=1) is True
        assert buff_cb.flush() is True
    assert buff_cb._thread.is_alive()


def test_flush_timeout(make_wrapper):
    """flush() returns False when the buffer can not drain within the timeout."""
    cb = BlockingCallback()
    buff_cb = make_wrapper(cb)

    buff_cb("event", {"seq_num": 0})
    assert buff_cb.flush(timeout=0.2) is False  # worker is blocked

    cb.release.set()
    assert buff_cb.flush(timeout=5) is True
    assert len(cb.called) == 1


def test_flush_with_raising_callback(make_wrapper):
    """Documents whose processing raises still count as processed: flush
    must not hang on them."""
    buff_cb = make_wrapper(CallbackWithException())
    for _ in range(5):
        buff_cb("event", {})
    assert buff_cb.flush(timeout=5) is True
    assert buff_cb._thread.is_alive()


def test_flush_after_shutdown(make_wrapper):
    """After a clean shutdown everything has been processed; flush returns True."""
    cb = SlowDummyCallback(delay=0.01)
    buff_cb = make_wrapper(cb)
    for i in range(5):
        buff_cb("event", {"seq_num": i})
    buff_cb.shutdown(wait=True)

    assert buff_cb.flush(timeout=1) is True
    assert buff_cb.flush(timeout=1) is True  # repeated calls remain safe
    assert len(cb.called) == 5


def test_read_after_write_pattern(make_wrapper):
    """The canonical use: write documents, flush, then read back."""
    store = {}

    def writer(name, doc):
        if name == "stop":
            store[doc["run_start"]] = "complete"

    buff_cb = make_wrapper(writer)
    buff_cb("start", {"uid": "abc"})
    buff_cb("stop", {"run_start": "abc"})

    assert buff_cb.flush(timeout=5) is True
    assert store.get("abc") == "complete"  # guaranteed visible after flush

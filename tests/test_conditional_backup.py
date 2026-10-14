"""Tests for `_ConditionalBackup` -- the primary/backup document router used by
`TiledWriter` and `TiledInserter` when a backup sink is configured.

The critical guarantee: once the primary callback fails for a run, it must not
receive any further documents of that run (in particular the Stop document), so
a partially written run can never masquerade as a complete one. The full
document stream is preserved in the backup sinks for later replay.
"""

import bluesky.plans as bp
import ophyd.sim

from bluesky_tiled_plugins import TiledWriter
from bluesky_tiled_plugins.writing.tiled_writer import _ConditionalBackup


class _Recorder:
    """A callback that records documents and optionally fails on one of them."""

    def __init__(self, fail_on: tuple[str, int] | None = None):
        # fail_on = (document name, 0-based occurrence index) to raise on
        self.received: list[tuple[str, dict]] = []
        self.fail_on = fail_on
        self._counts: dict[str, int] = {}

    def __call__(self, name, doc):
        count = self._counts.get(name, 0)
        self._counts[name] = count + 1
        if self.fail_on is not None and (name, count) == self.fail_on:
            raise RuntimeError(f"simulated failure on {name}[{count}]")
        self.received.append((name, doc))


DOCS = [
    ("start", {"uid": "st"}),
    ("descriptor", {"uid": "d1"}),
    ("event", {"uid": "e1"}),
    ("event", {"uid": "e2"}),
    ("stop", {"uid": "sp", "run_start": "st"}),
]


def test_no_failure_primary_gets_all_backup_gets_none():
    primary = _Recorder()
    backup = _Recorder()
    cb = _ConditionalBackup(primary, [backup])
    for name, doc in DOCS:
        cb(name, doc)
    assert [n for n, _ in primary.received] == [n for n, _ in DOCS]
    assert backup.received == []


def test_primary_abandoned_after_first_failure():
    """After the primary fails mid-run, it receives nothing further -- notably
    not the stop document -- while the backups receive the complete run."""
    primary = _Recorder(fail_on=("event", 0))  # fail on the first event
    backup = _Recorder()
    cb = _ConditionalBackup(primary, [backup])
    for name, doc in DOCS:
        cb(name, doc)  # must not raise

    # Primary got only the documents before the failure.
    assert [n for n, _ in primary.received] == ["start", "descriptor"]
    assert "stop" not in [n for n, _ in primary.received]

    # Backup got the complete run, in order, exactly once.
    assert [n for n, _ in backup.received] == [n for n, _ in DOCS]


def test_failure_on_stop_still_backs_up_whole_run():
    primary = _Recorder(fail_on=("stop", 0))
    backup = _Recorder()
    cb = _ConditionalBackup(primary, [backup])
    for name, doc in DOCS:
        cb(name, doc)
    assert [n for n, _ in primary.received] == ["start", "descriptor", "event", "event"]
    assert [n for n, _ in backup.received] == [n for n, _ in DOCS]


def test_backup_failure_is_contained():
    "A failing backup callback does not disturb the other backups."
    primary = _Recorder(fail_on=("start", 0))
    failing_backup = _Recorder(fail_on=("event", 1))
    good_backup = _Recorder()
    cb = _ConditionalBackup(primary, [failing_backup, good_backup])
    for name, doc in DOCS:
        cb(name, doc)  # must not raise
    assert [n for n, _ in good_backup.received] == [n for n, _ in DOCS]


def test_error_is_logged_with_traceback(caplog):
    primary = _Recorder(fail_on=("descriptor", 0))
    cb = _ConditionalBackup(primary, [_Recorder()])
    import logging

    with caplog.at_level(logging.ERROR, logger="bluesky_tiled_plugins"):
        for name, doc in DOCS:
            cb(name, doc)
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, "primary failure must be logged at ERROR level"
    assert any(r.exc_info for r in error_records), "traceback must be included"


def test_tiled_writer_failed_run_has_no_stop_and_full_backup(RE, client):
    """End-to-end: when writing fails mid-run, the Tiled run must NOT get a
    stop document (no partial run masquerading as complete), and the backup
    dictionary must hold the complete document stream."""
    backup_store: dict = {}

    def boom(doc):
        raise RuntimeError("simulated failure in descriptor handling")

    tw = TiledWriter(
        client, backup_dictionary=backup_store, patches={"descriptor": boom}
    )
    RE(bp.count([ophyd.sim.det], 3), tw)

    # The run container was created (start was processed) ...
    run = client.values().last()
    assert "start" in run.metadata
    # ... but the failure at the descriptor stopped all further writes:
    assert "stop" not in run.metadata, "a failed run must not receive a stop document"
    assert len(list(run)) == 0, "no streams must be written after the failure"

    # The full run (start through stop) is preserved in the backup store.
    (entries,) = backup_store.values()
    names = [entry["name"] for entry in entries]
    assert names[0] == "start"
    assert "stop" in names
    assert names.count("descriptor") >= 1
    n_events = sum(1 for name in names if name in {"event", "event_page"})
    assert n_events == 3


def test_tiled_writer_clean_run_leaves_backup_empty(RE, client):
    backup_store: dict = {}
    tw = TiledWriter(client, backup_dictionary=backup_store)
    RE(bp.count([ophyd.sim.det]), tw)

    run = client.values().last()
    assert "stop" in run.metadata
    assert backup_store == {}

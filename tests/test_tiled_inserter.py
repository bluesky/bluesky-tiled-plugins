"""Tests for `TiledInserter` -- a callback that mimics Databroker's
`insert` interface, posting plain Bluesky documents to a Mongo-backed
Tiled server and (optionally) buffering them to a backup sink if the
primary insertion fails.
"""

import json
import time
import uuid

import bluesky.plans as bp
import ophyd.sim
import pytest  # noqa: F401  (pytest fixtures are picked up via conftest.py)

from bluesky_tiled_plugins.writing.tiled_writer import TiledInserter

from examples.render import render_templated_documents


class _FailingClient:
    """A stand-in for `CatalogOfBlueskyRuns` whose `post_document`
    optionally delegates to a real client for the first `fail_after`
    calls and then raises on every subsequent call. Used to exercise
    the `_ConditionalBackup` fallback at various points in the document
    stream.

    `fail_after=0` (default) reproduces "fail immediately" semantics
    (every call raises). Passing a high `fail_after` and a real
    `wrapped` client lets the entire run land in the wrapped catalog
    without ever triggering the backup.
    """

    def __init__(self, wrapped=None, fail_after: int = 0):
        self.wrapped = wrapped
        self.fail_after = fail_after
        self.n_calls = 0

    def post_document(self, name, doc):
        if self.n_calls >= self.fail_after:
            self.n_calls += 1
            raise RuntimeError(
                f"simulated outage on call #{self.n_calls} (name={name!r})"
            )
        self.n_calls += 1
        if self.wrapped is not None:
            self.wrapped.post_document(name, doc)


def _assert_complete_run(store_entries):
    """Assert that `store_entries` -- a list of `{"name", "doc"}` records
    -- contains at least one start and one stop document with consistent
    uids (each `stop` document points back to its `start` via
    `stop["doc"]["run_start"]`)."""
    starts = [e for e in store_entries if e["name"] == "start"]
    stops = [e for e in store_entries if e["name"] == "stop"]
    assert starts, "no start document was backed up"
    assert stops, "no stop document was backed up"
    start_uids = {s["doc"]["uid"] for s in starts}
    stop_refs = {s["doc"]["run_start"] for s in stops}
    assert stop_refs <= start_uids, (
        f"stop documents reference run_start uids {stop_refs} that are not "
        f"among the backed-up start uids {start_uids}"
    )


def _read_jsonl_backup(tmp_path):
    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1, f"expected exactly one backup file, got {files}"
    with open(files[0]) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_lookup_by_scan_id_returns_newest_duplicate(mongo_catalog_client):
    """`catalog[scan_id]` must return the most recent run when multiple
    runs share the same `scan_id`. This exercises `_lookup_by_scan_id`,
    which does a server-side search and picks the newest match using
    the catalog's default sort direction. A prior "hardening" broke
    both by (a) passing a sort spec in the wrong shape and (b) sorting
    by a nested key that the mongo-normalized adapter cannot traverse,
    so this test guards against regressing to either failure mode.
    """
    scan_id = 42
    old_uid = uuid.uuid4().hex
    new_uid = uuid.uuid4().hex
    t_old = time.time()
    t_new = t_old + 100.0

    for uid, t in [(old_uid, t_old), (new_uid, t_new)]:
        mongo_catalog_client.post_document(
            "start", {"uid": uid, "time": t, "scan_id": scan_id}
        )
        mongo_catalog_client.post_document(
            "stop",
            {
                "uid": uuid.uuid4().hex,
                "time": t + 1.0,
                "run_start": uid,
                "exit_status": "success",
                "reason": "",
                "num_events": {},
            },
        )

    run = mongo_catalog_client[scan_id]
    assert run.start["uid"] == new_uid


def test_insert_round_trip(RE, mongo_catalog_client):
    """Documents inserted via TiledInserter must be retrievable from the
    Tiled server that backs the `CatalogOfBlueskyRuns` client."""
    inserter = TiledInserter(name="test", client=mongo_catalog_client)
    RE(bp.count([ophyd.sim.det], 3), inserter)

    runs = list(mongo_catalog_client.values())
    assert len(runs) == 1
    assert runs[0].stop is not None
    assert runs[0].stop["run_start"] == runs[0].start["uid"]


def test_insert_method_alias(RE, mongo_catalog_client):
    """`TiledInserter.insert(name, doc)` should behave the same as
    `TiledInserter.__call__`."""
    inserter = TiledInserter(name="test", client=mongo_catalog_client)
    RE(bp.count([ophyd.sim.det], 3), inserter.insert)

    runs = list(mongo_catalog_client.values())
    assert len(runs) == 1


@pytest.mark.parametrize(
    "fname, skip_keys",
    [
        # `internal_events`:
        #   `empty`: declared with shape `[]` but rows have actual shape
        #     `(0,)`, which MongoAdapter can't reshape back to a scalar.
        #   The fixture also contains a `ragged` data key with shape
        #     `[2, None]`. `databroker.mongo_normalized` has no
        #     ragged-array support: `GET /metadata/<run>/primary/data`
        #     eagerly builds an `ArrayStructure` for every child, and
        #     dask `normalize_chunks` rejects `None` dims, so the data
        #     container is unreachable. A per-key skip via `skip_keys`
        #     is not enough -- we mark this parametrization xfail until
        #     `mongo_normalized` learns to handle ragged shapes.
        pytest.param(
            "internal_events",
            {"empty", "ragged"},
            marks=pytest.mark.xfail(
                strict=True,
                reason=(
                    "databroker.mongo_normalized cannot build the `data` "
                    "container when any field has a None dim (ragged)."
                ),
                raises=Exception,
            ),
        ),
        # `external_assets_legacy`: external data keys reference the
        # `AD_HDF5_SWMR_STREAM` resource spec, for which MongoAdapter's
        # Filler has no handler registered in this test environment.
        ("external_assets_legacy", {"det-key1", "det-key2"}),
    ],
)
def test_insert_rendered_documents(
    mongo_catalog_client, external_assets_folder, fname, skip_keys
):
    """Drive `TiledInserter` with rendered example documents (rather
    than live RE-generated docs) and read every supported data key back
    from the Mongo-backed catalog. `skip_keys` lists data keys that
    `TiledInserter` inserts successfully but that MongoAdapter cannot
    serve on the read side for reasons documented in the parametrize
    block above."""
    inserter = TiledInserter(name="test", client=mongo_catalog_client)
    uid = None
    for item in render_templated_documents(fname + ".json", external_assets_folder):
        if item["name"] == "start":
            uid = item["doc"]["uid"]
        inserter(item["name"], item["doc"])

    assert uid is not None
    run = mongo_catalog_client[uid]
    assert run.start["uid"] == uid
    assert run.stop is not None
    assert run.stop["run_start"] == uid

    # Read back every supported data key from every stream
    assert len(run.items()) > 0
    for stream in run.values():
        data = stream["data"]
        read_keys = [k for k in data if k not in skip_keys]
        assert len(read_keys) > 0
        for key in read_keys:
            arr = data[key].read()
            assert arr is not None and arr.size > 0


def test_no_backup_when_primary_succeeds(RE, mongo_catalog_client, tmp_path):
    inserter = TiledInserter(
        name="test",
        client=mongo_catalog_client,
        backup_directory=str(tmp_path),
    )
    RE(bp.count([ophyd.sim.det], 3), inserter)

    assert list(tmp_path.glob("*.jsonl")) == []


def test_backup_directory_used_on_failure(RE, tmp_path):
    """Smoke test: with a totally-failing primary client, a directory-
    only backup gets a complete run."""
    inserter = TiledInserter(
        name="test",
        client=_FailingClient(),
        backup_directory=str(tmp_path),
    )
    RE(bp.count([ophyd.sim.det], 3), inserter)

    _assert_complete_run(_read_jsonl_backup(tmp_path))


def test_backup_dictionary_used_on_failure(RE, redis_json_dict_store):
    """When the primary client fails and `backup_dictionary` is set,
    documents are flushed to a `JSONDictWriter` writing into that dict."""
    inserter = TiledInserter(
        name="test",
        client=_FailingClient(),
        backup_dictionary=redis_json_dict_store,
    )
    RE(bp.count([ophyd.sim.det], 3), inserter)

    assert len(redis_json_dict_store) == 1
    (entries,) = redis_json_dict_store.values()
    _assert_complete_run(list(entries))


# A `bp.count([det], 3)` run emits 6 documents in this order:
#   start, descriptor, event, event, event, stop
# So `fail_after`:
#   0  -> raises on the start doc (fail before anything lands)
#   1  -> raises on the descriptor (fail right after start)
#   3  -> raises on the second event (fail mid-run)
#   6  -> raises only on a hypothetical 7th call; since there isn't
#          one, the primary client never fails and the backups never
#          engage -- the run is fully posted to the wrapped catalog.
_N_DOCS_IN_COUNT_3 = 6


@pytest.mark.parametrize(
    "fail_after",
    [0, 1, 3, _N_DOCS_IN_COUNT_3],
    ids=["fail_on_start", "fail_after_start", "fail_mid_run", "fail_after_stop"],
)
def test_backup_to_both_sinks_on_failure(
    RE,
    mongo_catalog_client,
    tmp_path,
    redis_json_dict_store,
    fail_after,
):
    """End-to-end backup test: a primary client that delegates the
    first `fail_after` calls to `mongo_catalog_client` and raises
    thereafter, wired to a `TiledInserter` configured with *both*
    `backup_directory` and `backup_dictionary` sinks.

    For every failure point we expect:
      - the complete run in *both* backup sinks (or in neither, if
        `fail_after` is large enough that no failure ever triggers);
      - the wrapped catalog holds a complete run only when no failure
        triggered; otherwise it holds at most a partial, stop-less run.

    Auto-parametrized over `mongomock`/real `mongo` and over
    `fakeredis`/real `redis` via the underlying fixtures.
    """
    expect_backup = fail_after < _N_DOCS_IN_COUNT_3

    client = _FailingClient(wrapped=mongo_catalog_client, fail_after=fail_after)
    inserter = TiledInserter(
        name="test",
        client=client,
        backup_directory=str(tmp_path),
        backup_dictionary=redis_json_dict_store,
    )
    RE(bp.count([ophyd.sim.det], 3), inserter)

    backup_files = list(tmp_path.glob("*.jsonl"))
    if expect_backup:
        _assert_complete_run(_read_jsonl_backup(tmp_path))
        assert len(redis_json_dict_store) == 1
        (entries,) = redis_json_dict_store.values()
        _assert_complete_run(list(entries))
    else:
        assert backup_files == [], f"expected no backup file, got {backup_files}"
        assert len(redis_json_dict_store) == 0, (
            f"expected empty backup dict, got keys {list(redis_json_dict_store)}"
        )

    runs = list(mongo_catalog_client.values())
    if not expect_backup:
        assert len(runs) == 1
        assert runs[0].stop is not None
        assert runs[0].stop["run_start"] == runs[0].start["uid"]
    else:
        for run in runs:
            assert run.stop is None, (
                f"unexpected complete run in catalog for fail_after={fail_after}"
            )

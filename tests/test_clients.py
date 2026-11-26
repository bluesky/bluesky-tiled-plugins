import asyncio
import json
import uuid
from datetime import datetime

import numpy as np
import pytest
from examples.render import render_templated_documents
from tiled.client import record_history
from tiled.queries import KeyLike, KeyLookup, Like
from tiled.structures.core import Spec

from bluesky_tiled_plugins import TiledWriter
from bluesky_tiled_plugins.clients.bluesky_run import _iter_json_seq
from bluesky_tiled_plugins.exporters import json_seq_exporter


@pytest.fixture(scope="module", params=["internal_events", "external_assets"])
def run_client(client, external_assets_folder, request):
    tw = TiledWriter(client)
    for item in render_templated_documents(
        request.param + ".json", external_assets_folder
    ):
        if item["name"] == "start":
            uid = item["doc"]["uid"]
        tw(**item)

    yield client[uid]


def test_documents(run_client):
    assert len(list(run_client.v3.documents())) > 0
    assert len(list(run_client.v2.documents())) > 0


@pytest.mark.parametrize(
    "framing",
    [
        pytest.param(("\x1e", "\n"), id="rfc7464"),
        pytest.param(("", "\n"), id="legacy-ndjson"),
    ],
)
def test_iter_json_seq_accepts_both_framings(framing):
    """The client parser must accept both RFC 7464 (\\x1E-prefixed) and
    legacy NDJSON records so a new client can talk to an older server
    during a rolling upgrade. Chunk boundaries are placed at arbitrary
    byte offsets to exercise the incremental buffering.
    """
    prefix, terminator = framing
    docs = [
        ("start", {"uid": "u0", "time": 0.0}),
        ("stop", {"uid": "u1", "run_start": "u0", "time": 1.0}),
    ]
    payload = "".join(
        prefix + json.dumps({"name": name, "doc": doc}) + terminator
        for name, doc in docs
    ).encode()
    chunks = [payload[i : i + 7] for i in range(0, len(payload), 7)]
    parsed = list(_iter_json_seq(chunks))
    assert [name for name, _ in parsed] == [name for name, _ in docs]


def test_reversed_iteration(run_client):
    """`keys()` and `items()` views must support reverse slicing on both a
    BlueskyRun (stream names) and a `CompositeSubsetClient` (data-key
    subset). Slicing the KeysView directly (not a materialized list) is
    what exercises the client's `_keys_slice` / `_items_slice` overrides.
    """
    forward_stream_names = list(run_client.keys())
    assert list(run_client.keys()[::-1]) == forward_stream_names[::-1]
    assert [k for k, _ in run_client.items()[::-1]] == forward_stream_names[::-1]

    data = run_client.v2[forward_stream_names[0]]["data"]
    forward_data_keys = list(data.keys())
    assert list(data.keys()[::-1]) == forward_data_keys[::-1]
    assert [k for k, _ in data.items()[::-1]] == forward_data_keys[::-1]


@pytest.mark.parametrize("fixture_name", ["internal_events", "external_assets"])
def test_export_roundtrip_preserves_structure(
    client, external_assets_folder, fixture_name
):
    """Export a run via `application/json-seq` and re-ingest it through
    `TiledWriter`; the roundtripped run must have the same stream layout
    and per-key shapes as the original.

    `max_array_size=-1` keeps all internal array data internal so the
    exporter does not have to synthesize stream_resource parameters for
    generated zarr assets.
    """
    tw = TiledWriter(client, max_array_size=-1)
    original_uid = None
    for item in render_templated_documents(
        f"{fixture_name}.json", external_assets_folder
    ):
        if item["name"] == "start":
            original_uid = item["doc"]["uid"]
        tw(**item)

    original = client[original_uid]
    docs = list(original.v3.documents())

    new_uid = uuid.uuid4().hex
    tw2 = TiledWriter(client, max_array_size=-1)
    for name, doc in docs:
        d = dict(doc)
        if name == "start":
            d["uid"] = new_uid
        elif d.get("run_start") == original_uid:
            d["run_start"] = new_uid
        tw2(name=name, doc=d)

    roundtripped = client[new_uid]

    assert set(original.keys()) == set(roundtripped.keys())
    for stream in original.keys():
        assert set(original[stream].keys()) == set(roundtripped[stream].keys())
        for key in original[stream].keys():
            assert original[stream][key].shape == roundtripped[stream][key].shape


def test_export_nd_numeric_tabular_array(client):
    """A 2D numeric array small enough to stay in the internal (SQL) table
    is read back from the table as a numpy object array whose elements are
    themselves numpy arrays. The JSON-Seq exporter must recursively convert
    it to nested builtin lists; otherwise it raises `Object of type ndarray
    is not JSON serializable` while serializing the reconstructed event.
    """
    uid = uuid.uuid4().hex
    desc_uid = uuid.uuid4().hex
    img = [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
    docs = [
        ("start", {"uid": uid, "time": 0.0}),
        (
            "descriptor",
            {
                "uid": desc_uid,
                "run_start": uid,
                "time": 0.0,
                "name": "primary",
                "data_keys": {
                    "img": {
                        "source": "sim",
                        "dtype": "array",
                        "dtype_numpy": "<f8",
                        "shape": [2, 3],
                        "object_name": "det",
                    },
                },
                "object_keys": {"det": ["img"]},
            },
        ),
        (
            "event",
            {
                "uid": uuid.uuid4().hex,
                "descriptor": desc_uid,
                "time": 0.0,
                "seq_num": 1,
                "data": {"img": img},
                "timestamps": {"img": 0.0},
                "filled": {},
            },
        ),
        (
            "stop",
            {
                "uid": uuid.uuid4().hex,
                "run_start": uid,
                "time": 1.0,
                "exit_status": "success",
                "num_events": {"primary": 1},
            },
        ),
    ]

    # The default max_array_size keeps the 2x3 array in the tabular store
    # rather than writing it as a separate zarr node.
    tw = TiledWriter(client)
    for name, doc in docs:
        tw(name=name, doc=doc)

    run = client[uid]
    assert "img" not in run["primary"].base

    events = [doc for name, doc in run.v3.documents() if name == "event"]
    assert len(events) == 1
    assert events[0]["data"]["img"] == img


def test_export_non_builtin_types():
    """The JSON-Seq exporter must serialize numpy arrays, numpy scalars and
    datetime objects, none of which the builtin `json` module accepts.

    This drives the exporter directly, with a stand-in for a run that has
    no streams, so that the values reach the serializer exactly as given.
    """

    class EmptyRun:
        specs = [Spec("BlueskyRun", version="3.0")]

        async def keys_range(self, offset, limit):
            return []

    async def filter_for_access(adapter):
        return adapter

    metadata = {
        "start": {
            "uid": "u0",
            "when": datetime(2026, 1, 2, 3, 4, 5),
            "positions": np.arange(3),
            "num_points": np.int64(3),
            "units": "\u00b5m",
        },
        "stop": {"uid": "u1", "run_start": "u0", "time": np.float64(1.0)},
    }

    async def export():
        return [
            record
            async for record in json_seq_exporter(
                "application/json-seq", EmptyRun(), metadata, filter_for_access
            )
        ]

    records = asyncio.run(export())
    # RFC 7464 framing, as text.
    assert all(isinstance(record, str) for record in records)
    assert all(record[0] == "\x1e" and record[-1] == "\n" for record in records)

    payload = "".join(records).encode("utf-8")
    assert list(_iter_json_seq([payload])) == [
        (
            "start",
            {
                "uid": "u0",
                "when": "2026-01-02T03:04:05",
                "positions": [0, 1, 2],
                "num_points": 3,
                "units": "\u00b5m",
            },
        ),
        ("stop", {"uid": "u1", "run_start": "u0", "time": 1.0}),
    ]


@pytest.fixture(scope="module")
def runs_catalog(client):
    """A SQL-backed `CatalogOfBlueskyRuns`; its first two runs share a uid prefix."""
    # Unique name: the catalog is shared across the parametrized `app` fixture.
    name = f"runs_{uuid.uuid4().hex[:8]}"
    client.create_container(name, specs=[Spec("CatalogOfBlueskyRuns", version="3.0")])
    runs = client[name]
    # A run's node key is its uid. These two share a 5-character prefix, so a
    # 5-character partial uid is ambiguous while a 6-character one is unique.
    uids = ["abcde1" + uuid.uuid4().hex[6:], "abcde2" + uuid.uuid4().hex[6:]]
    # A uid as bluesky generates it: a UUID4 with hyphens, 36 characters.
    uids.append("12345678" + str(uuid.uuid4())[8:])
    # A key that is longer than a UUID4, so its first 36 characters look like a
    # full uid but are only part of the key.
    uids.append("87654321" + str(uuid.uuid4())[8:] + "-long")
    # Keys that are not uids at all: one shorter than the shortest partial uid
    # allowed, and one that another key starts with.
    uids.extend(["xy", "scan1", "scan10"])
    for uid in uids:
        runs.create_container(
            uid, metadata={"start": {"uid": uid}}, specs=[Spec("BlueskyRun", "3.0")]
        )
    return runs, uids


@pytest.mark.parametrize("server_has_key_like", [True, False])
def test_partial_uid_lookup(runs_catalog, monkeypatch, server_has_key_like):
    """KeyLike (on the run key) when the server supports it, else Like on metadata."""
    runs, uids = runs_catalog
    if not server_has_key_like:
        monkeypatch.setattr(
            runs.context.server_info,
            "queries",
            [q for q in runs.context.server_info.queries if q != "key_like"],
        )
    # A fresh client, so that the cached support check is recomputed.
    runs = type(runs)(
        runs.context, item=runs.item, structure_clients=runs.structure_clients
    )

    if server_has_key_like:
        expected_query = KeyLike("abcde1", before=False)
    else:
        expected_query = Like("start.uid", "abcde1%")
    assert runs._partial_uid_query("abcde1") == expected_query

    assert runs["abcde1"].metadata["start"]["uid"] == uids[0]
    assert runs.uid["abcde2"].metadata["start"]["uid"] == uids[1]
    assert runs[uids[0]].metadata["start"]["uid"] == uids[0]  # full uid


@pytest.mark.parametrize(
    "partial_uid, error, match, num_requests",
    [
        # Ambiguous or too short as a partial uid, and not the key of a run
        # either: the error of the partial uid lookup is the one raised.
        ("abcde", ValueError, "multiple matches", 2),
        ("abc", ValueError, "too short", 1),
        # An empty key is never looked up: it would fetch the catalog itself.
        ("", ValueError, "too short", 0),
        ("/", ValueError, "too short", 0),
        # Nothing starts with this, so there is no key to look up exactly.
        ("zzzzz", KeyError, None, 1),
    ],
)
def test_partial_uid_lookup_errors(
    runs_catalog, partial_uid, error, match, num_requests
):
    runs, _ = runs_catalog
    with record_history() as history:
        with pytest.raises(error, match=match):
            runs[partial_uid]
    assert len(history.requests) == num_requests


def _lookup(runs, key):
    """Look up `key`; return the uid of the run and the requests it took."""
    with record_history() as history:
        uid = runs[key].metadata["start"]["uid"]
    return uid, [request.url.path for request in history.requests]


def test_uid_lookup_is_one_request(runs_catalog):
    """
    The length of the key picks the lookup that is tried first: a fetch of the
    run for a full uid (36 characters), a search for anything shorter. Each is
    right for its own case, so neither costs more than a single request.
    """
    runs, uids = runs_catalog
    full_uid = uids[2]
    assert len(full_uid) == 36

    uid, (path,) = _lookup(runs, full_uid)
    assert uid == full_uid
    assert "/metadata/" in path

    uid, (path,) = _lookup(runs, full_uid[:8])
    assert uid == full_uid
    assert "/search/" in path


def test_full_length_key_falls_back_to_partial_uid_lookup(runs_catalog):
    """
    A 36-character key that is not the whole key is still found, as a partial
    uid. Only then does the lookup take a second request.
    """
    runs, uids = runs_catalog
    long_uid = uids[3]

    uid, (first, second) = _lookup(runs, long_uid[:36])
    assert uid == long_uid
    assert "/metadata/" in first
    assert "/search/" in second


def test_key_that_is_not_a_uid_is_reachable(runs_catalog):
    """
    A key that fails as a partial uid, because it is too short or because
    other keys start with it, is looked up as the exact key before giving up.
    """
    runs, _ = runs_catalog

    # Too short for a partial uid: no search is made, only the fetch.
    uid, (path,) = _lookup(runs, "xy")
    assert uid == "xy"
    assert "/metadata/" in path

    # "scan10" starts with it too: the search is ambiguous, the fetch is not.
    uid, (first, second) = _lookup(runs, "scan1")
    assert uid == "scan1"
    assert "/search/" in first
    assert "/metadata/" in second

    # Nothing else starts with this, so it is found as a partial uid would be.
    uid, (path,) = _lookup(runs, "scan10")
    assert uid == "scan10"
    assert "/search/" in path

    # The explicit partial uid accessor does not fall back.
    with pytest.raises(ValueError, match="multiple matches"):
        runs.uid["scan1"]
    with pytest.raises(ValueError, match="too short"):
        runs.uid["xy"]


def test_search_does_not_rewrite_node_key_queries(runs_catalog):
    """
    `search` prefixes metadata keys with 'start.'; the key of KeyLike and
    KeyLookup is the node key (the run uid) and must be passed through as is.
    """
    runs, uids = runs_catalog
    assert list(runs.search(KeyLike("abcde1", before=False))) == [uids[0]]
    assert list(runs.search(KeyLookup(uids[1]))) == [uids[1]]

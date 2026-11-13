import json
import uuid

import pytest
from examples.render import render_templated_documents
from tiled.queries import KeyLike, KeyLookup, Like
from tiled.structures.core import Spec

from bluesky_tiled_plugins import TiledWriter
from bluesky_tiled_plugins.clients.bluesky_run import _iter_json_seq


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


@pytest.fixture(scope="module")
def runs_catalog(client):
    """A SQL-backed `CatalogOfBlueskyRuns` holding two runs with a shared uid prefix."""
    # Unique name: the catalog is shared across the parametrized `app` fixture.
    name = f"runs_{uuid.uuid4().hex[:8]}"
    client.create_container(name, specs=[Spec("CatalogOfBlueskyRuns", version="3.0")])
    runs = client[name]
    # A run's node key is its uid. The two share a 5-character prefix, so a
    # 5-character partial uid is ambiguous while a 6-character one is unique.
    uids = ["abcde1" + uuid.uuid4().hex[6:], "abcde2" + uuid.uuid4().hex[6:]]
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
    "partial_uid, error, match",
    [
        ("abcde", ValueError, "multiple matches"),
        ("zzzzz", KeyError, None),
        ("abc", ValueError, "too short"),
    ],
)
def test_partial_uid_lookup_errors(runs_catalog, partial_uid, error, match):
    runs, _ = runs_catalog
    with pytest.raises(error, match=match):
        runs[partial_uid]


def test_search_does_not_rewrite_node_key_queries(runs_catalog):
    """
    `search` prefixes metadata keys with 'start.'; the key of KeyLike and
    KeyLookup is the node key (the run uid) and must be passed through as is.
    """
    runs, uids = runs_catalog
    assert list(runs.search(KeyLike("abcde1", before=False))) == [uids[0]]
    assert list(runs.search(KeyLookup(uids[1]))) == [uids[1]]

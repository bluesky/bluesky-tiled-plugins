import json
import uuid

import pytest
from examples.render import render_templated_documents

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

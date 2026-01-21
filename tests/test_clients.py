import pytest
from examples.render import render_templated_documents

from bluesky_tiled_plugins import TiledWriter


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

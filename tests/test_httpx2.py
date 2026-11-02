"""Guard the httpx generation contract with the tiled client.

bluesky-tiled-plugins catches exceptions raised through the tiled client
(e.g. in the remote-validation 404 fallbacks), so the exception family it
catches must match the one tiled raises. Since tiled 0.2.21 that is httpx2;
this test fails loudly if tiled migrates again.
"""

import httpx2
from tiled.client.utils import ClientError


def test_tiled_client_error_is_catchable_via_httpx2():
    assert issubclass(ClientError, httpx2.HTTPStatusError)

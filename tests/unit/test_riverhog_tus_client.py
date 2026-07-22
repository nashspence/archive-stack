from __future__ import annotations

import httpx
import pytest
from riverhog_api_client import Conflict
from riverhog_api_client.tus import TusHttpClient
from tus_transport import TusTransport


@pytest.mark.parametrize("status", [404, 410])
def test_missing_tus_upload_requests_a_current_lease(status: int) -> None:
    transport = TusTransport(
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(status, content=b"upload not found")
            )
        )
    )
    client = TusHttpClient()
    client._transport = transport

    with pytest.raises(Conflict, match="requesting a current lease"):
        client.patch_chunk(
            "https://uploads.example/file",
            offset=0,
            checksum_algorithm="sha256",
            content=b"payload",
        )

    client.close()

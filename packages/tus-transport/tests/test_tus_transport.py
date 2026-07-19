from __future__ import annotations

import base64
import hashlib

import httpx
import pytest
from tus_transport import TusHttpError, TusTransport


def test_transport_reuses_supplied_client_and_supports_optional_checksums() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        offset = int(request.headers["Upload-Offset"])
        return httpx.Response(204, headers={"Upload-Offset": str(offset + len(request.content))})

    client = httpx.Client(transport=httpx.MockTransport(handle))
    transport = TusTransport(client=client, headers={"Authorization": "Bearer token"})

    assert transport.patch_chunk("http://uploads.test/file", offset=0, content=b"first") == 5
    assert (
        transport.patch_chunk(
            "http://uploads.test/file",
            offset=5,
            content=b"second",
            checksum_algorithm="sha256",
        )
        == 11
    )
    transport.close()

    assert len(requests) == 2
    assert all(request.headers["Authorization"] == "Bearer token" for request in requests)
    assert "Upload-Checksum" not in requests[0].headers
    expected = base64.b64encode(hashlib.sha256(b"second").digest()).decode("ascii")
    assert requests[1].headers["Upload-Checksum"] == f"sha256 {expected}"
    assert not client.is_closed
    client.close()


def test_transport_reports_http_status_and_body_neutrally() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(507, content=b"storage pressure")

    client = httpx.Client(transport=httpx.MockTransport(handle))
    transport = TusTransport(client=client)

    with pytest.raises(TusHttpError) as exc_info:
        transport.patch_chunk("http://uploads.test/file", offset=0, content=b"chunk")

    assert exc_info.value.status == 507
    assert exc_info.value.body == b"storage pressure"
    client.close()

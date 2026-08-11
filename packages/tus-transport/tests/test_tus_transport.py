from __future__ import annotations

import base64
import hashlib
from typing import Any

import httpx
import pytest
from tus_transport import TusHttpError, TusTransport


class FakeResponse:
    status = 204
    will_close = False

    def __init__(self, *, offset: int) -> None:
        self.offset = offset

    def read(self) -> bytes:
        return b""

    def getheaders(self) -> list[tuple[str, str]]:
        return [("Upload-Offset", str(self.offset))]


class FakeHttpConnection:
    instances: list[FakeHttpConnection] = []

    def __init__(self, host: str, *, port: int | None, timeout: float) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.requests: list[dict[str, Any]] = []
        self.closed = False
        self.instances.append(self)

    def request(
        self,
        method: str,
        target: str,
        *,
        body: bytes,
        headers: dict[str, str],
    ) -> None:
        self.requests.append(
            {
                "method": method,
                "target": target,
                "body": body,
                "headers": headers,
            }
        )

    def getresponse(self) -> FakeResponse:
        request = self.requests[-1]
        return FakeResponse(
            offset=int(request["headers"]["Upload-Offset"]) + len(request["body"]),
        )

    def close(self) -> None:
        self.closed = True


def test_transport_streams_patch_chunks_over_a_persistent_http11_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeHttpConnection.instances = []
    monkeypatch.setattr("tus_transport.http.client.HTTPConnection", FakeHttpConnection)
    transport = TusTransport(headers={"Authorization": "Bearer token"})

    assert (
        transport.patch_chunk("http://uploads.test/file?signature=x", offset=0, content=b"one") == 3
    )
    assert (
        transport.patch_chunk(
            "http://uploads.test/file?signature=x",
            offset=3,
            content=b"two",
            checksum_algorithm="sha256",
        )
        == 6
    )
    transport.close()

    assert len(FakeHttpConnection.instances) == 1
    connection = FakeHttpConnection.instances[0]
    assert connection.host == "uploads.test"
    assert connection.port is None
    assert connection.timeout == 300.0
    assert [request["target"] for request in connection.requests] == [
        "/file?signature=x",
        "/file?signature=x",
    ]
    assert [request["body"] for request in connection.requests] == [b"one", b"two"]
    assert all(
        request["headers"]["Authorization"] == "Bearer token" for request in connection.requests
    )
    expected = base64.b64encode(hashlib.sha256(b"two").digest()).decode("ascii")
    assert connection.requests[1]["headers"]["Upload-Checksum"] == f"sha256 {expected}"
    assert connection.closed


def test_transport_reuses_supplied_client_and_supports_optional_checksums() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        offset = int(request.headers["Upload-Offset"])
        return httpx.Response(204, headers={"Upload-Offset": str(offset + len(request.content))})

    client = httpx.Client(transport=httpx.MockTransport(handle))
    transport = TusTransport(
        client=client,
        patch_client=client,
        headers={"Authorization": "Bearer token"},
    )

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
    transport = TusTransport(client=client, patch_client=client)

    with pytest.raises(TusHttpError) as exc_info:
        transport.patch_chunk("http://uploads.test/file", offset=0, content=b"chunk")

    assert exc_info.value.status == 507
    assert exc_info.value.body == b"storage pressure"
    client.close()

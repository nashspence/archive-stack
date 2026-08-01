from __future__ import annotations

import httpx
from jeb_api_client.client import JebApiClient


def test_jeb_client_uses_its_own_persistent_authenticated_api() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/status":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v1/sources":
            return httpx.Response(200, json={"sources": []})
        if request.url.path == "/v1/attempts/attempt-1":
            return httpx.Response(200, json={"attempt_id": "attempt-1"})
        return httpx.Response(404, json={"error": {"message": "not found"}})

    client = JebApiClient(base_url="https://jeb.example.test", token="jeb-token")
    transport_client = httpx.Client(
        base_url=client.base_url,
        headers={"Authorization": "Bearer jeb-token"},
        transport=httpx.MockTransport(handle),
    )
    client._client = transport_client
    try:
        assert client.status() == {"status": "ok"}
        assert client.list_sources() == {"sources": []}
        assert client.get_attempt("attempt-1") == {"attempt_id": "attempt-1"}
        assert client._client is transport_client
    finally:
        client.close()

    assert [request.url.path for request in requests] == [
        "/v1/status",
        "/v1/sources",
        "/v1/attempts/attempt-1",
    ]
    assert all(request.headers["Authorization"] == "Bearer jeb-token" for request in requests)

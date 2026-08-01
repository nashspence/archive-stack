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
            return httpx.Response(
                200,
                json={
                    "attempt_id": "attempt-1",
                    "state": "canceled" if request.method == "DELETE" else "batching",
                },
            )
        if request.url.path == "/v1/operations":
            return httpx.Response(200, json={"operations": []})
        if request.url.path == "/v1/operations/op-1":
            return httpx.Response(200, json={"id": "op-1", "state": "succeeded"})
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
        assert client.get_attempt("attempt-1")["state"] == "batching"
        assert client.cancel_attempt("attempt-1")["state"] == "canceled"
        assert client.list_operations() == {"operations": []}
        assert client.get_operation("op-1")["state"] == "succeeded"
        assert client._client is transport_client
    finally:
        client.close()

    assert [request.url.path for request in requests] == [
        "/v1/status",
        "/v1/sources",
        "/v1/attempts/attempt-1",
        "/v1/attempts/attempt-1",
        "/v1/operations",
        "/v1/operations/op-1",
    ]
    assert all(request.headers["Authorization"] == "Bearer jeb-token" for request in requests)


def test_jeb_client_attempt_list_uses_resolution_vocabulary() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"attempts": []})

    client = JebApiClient(base_url="https://jeb.example.test")
    client._client = httpx.Client(base_url=client.base_url, transport=httpx.MockTransport(handle))
    try:
        assert client.list_attempts(resolution="resolved") == {"attempts": []}
    finally:
        client.close()

    assert requests[0].url.params["resolution"] == "resolved"


def test_jeb_client_waits_with_one_persistent_client_and_reports_state_changes(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    states = iter(("batching", "batching", "cleanup_done"))
    requests: list[httpx.Request] = []
    sleeps: list[float] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"attempt_id": "attempt-1", "state": next(states)},
        )

    monkeypatch.setattr("jeb_api_client.client.time.sleep", sleeps.append)
    client = JebApiClient(base_url="https://jeb.example.test")
    transport_client = httpx.Client(
        base_url=client.base_url,
        transport=httpx.MockTransport(handle),
    )
    client._client = transport_client
    updates: list[str] = []
    try:
        final = client.wait_for_attempt(
            "attempt-1",
            interval=2.5,
            on_update=lambda payload: updates.append(str(payload["state"])),
        )
        assert client._client is transport_client
    finally:
        client.close()

    assert final["state"] == "cleanup_done"
    assert updates == ["batching", "cleanup_done"]
    assert sleeps == [2.5, 2.5]
    assert [request.url.path for request in requests] == [
        "/v1/attempts/attempt-1",
        "/v1/attempts/attempt-1",
        "/v1/attempts/attempt-1",
    ]


def test_jeb_client_watch_retries_transport_and_stops_on_owned_failure(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    calls = 0
    sleeps: list[float] = []

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("temporary", request=request)
        return httpx.Response(200, json={"attempt_id": "attempt-1", "state": "failed"})

    monkeypatch.setattr("jeb_api_client.client.time.sleep", sleeps.append)
    client = JebApiClient(base_url="https://jeb.example.test")
    client._client = httpx.Client(base_url=client.base_url, transport=httpx.MockTransport(handle))
    try:
        final = client.wait_for_attempt("attempt-1", interval=1.0)
    finally:
        client.close()

    assert final["state"] == "failed"
    assert calls == 2
    assert sleeps == [1.0]

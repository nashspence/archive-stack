from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import httpx
import pytest
from jeb_api_client.client import JebApiClient, JebApiError, JebIngressClient
from riverhog_provenance import create_observation_journal, validate_journal


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
        return httpx.Response(
            404,
            json={"error": {"code": "not_found", "message": "not found"}},
        )

    client = JebApiClient(base_url="https://jeb.example.test", token="jeb-token")
    transport_client = httpx.Client(
        base_url=client.base_url,
        headers={"Authorization": "Bearer jeb-token"},
        transport=httpx.MockTransport(handle),
    )
    client._client = transport_client
    try:
        assert client.get_status() == {"status": "ok"}
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


def test_jeb_client_preserves_the_server_error_contract() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={
                "error": {
                    "code": "not_found",
                    "message": "source not found: missing",
                }
            },
        )

    client = JebApiClient(base_url="https://jeb.example.test")
    client._client = httpx.Client(
        base_url=client.base_url,
        transport=httpx.MockTransport(handle),
    )
    try:
        with pytest.raises(JebApiError, match="source not found: missing") as denied:
            client.get_source("missing")
    finally:
        client.close()

    assert denied.value.code == "not_found"
    assert denied.value.status == 404


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


def test_jeb_ingress_transports_signed_identities_and_exact_journal_separately(
    tmp_path: Path,
) -> None:
    payload = tmp_path / "movie.mov"
    payload.write_bytes(b"source movie")
    relative_path = "camera/movie.mov"
    journal = create_observation_journal(
        payload,
        relative_path=relative_path,
        host_id="urn:uuid:00000000-0000-0000-0000-000000000001",
        agent_name="jeb-client-test",
        agent_version="0.1.0",
    )
    summary = validate_journal(journal)
    binding = {
        "path": relative_path,
        "bytes": payload.stat().st_size,
        "sha256": hashlib.sha256(payload.read_bytes()).hexdigest(),
        "status": "captured",
        "journal_id": summary.journal_id,
        "current_state_id": summary.current_state_id,
    }
    upload_id = "a" * 32
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST" and request.url.path == "/files/":
            return httpx.Response(201, headers={"Location": f"/files/{upload_id}"})
        if request.method == "PUT" and request.url.path.startswith("/provenance/"):
            return httpx.Response(200, json={"status": "accepted"})
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Upload-Offset": "0"})
        if request.method == "PATCH":
            return httpx.Response(
                204,
                headers={"Upload-Offset": str(payload.stat().st_size)},
            )
        return httpx.Response(404)

    client = JebIngressClient(
        source="phone",
        password="secret",
        base_url="https://jeb.example.test",
        transport=httpx.MockTransport(handle),
    )
    try:
        result = client.upload_file(
            payload,
            relative_path=relative_path,
            binding=binding,
            journals={summary.journal_id: journal},
        )
    finally:
        client.close()

    assert result["upload_id"] == upload_id
    create = requests[0]
    metadata = {
        key: base64.b64decode(value).decode("utf-8")
        for item in create.headers["Upload-Metadata"].split(",")
        for key, value in [item.split(" ", 1)]
    }
    assert metadata["path"] == relative_path
    assert metadata["sha256"] == binding["sha256"]
    assert len(metadata["provenance_sha256"]) == 64
    journal_request = next(request for request in requests if "/journals/" in request.url.path)
    assert journal_request.content == journal
    assert (
        journal_request.headers["X-Riverhog-Provenance-SHA256"]
        == hashlib.sha256(journal).hexdigest()
    )
    assert [request.method for request in requests] == ["POST", "PUT", "PUT", "HEAD", "PATCH"]

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Iterable, Iterator
from typing import cast

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from http_api_contracts import FRAMED_REQUEST_MEDIA_TYPE
from riverhog_storage_adapter_asgi_support import create_storage_adapter_app
from riverhog_storage_adapter_protocol import (
    AdapterDescriptor,
    ImmutableObjectReceipt,
    ObjectLocator,
    ObjectReadRequest,
    SmallObjectWriteRequest,
    StorageAdapterPort,
)
from riverhog_storage_adapter_support import framed_request, framed_request_length


class _Adapter:
    def __init__(self) -> None:
        self.descriptor_calls = 0
        self.upload_chunks: list[bytes] = []

    def descriptor(self) -> AdapterDescriptor:
        self.descriptor_calls += 1
        return AdapterDescriptor(
            implementation_id="fixture.storage/v1",
            implementation_version="1.0.0",
            read_mode="immediate",
            minimum_nonfinal_segment_bytes=1,
            maximum_segment_bytes=1024,
            maximum_segment_count=10,
        )

    def iter_object(self, request: ObjectReadRequest) -> Iterator[bytes]:
        assert request.object.object_path == "objects/item"
        yield b"streamed"
        yield b"-bytes"

    def put_small_object(
        self,
        request: SmallObjectWriteRequest,
        content: bytes | Iterable[bytes],
    ) -> ImmutableObjectReceipt:
        chunks = (content,) if isinstance(content, bytes) else content
        self.upload_chunks = [chunk for chunk in chunks if chunk]
        stored = b"".join(self.upload_chunks)
        assert len(stored) == request.stored_bytes
        assert hashlib.sha256(stored).hexdigest() == request.stored_sha256
        return ImmutableObjectReceipt(
            object_path=request.object_path,
            stored_bytes=len(stored),
            stored_sha256=request.stored_sha256,
            verified_identity_assertions=request.required_identity_assertions,
            verified_placement=request.placement,
            completed_at="2026-08-25T00:00:00.000000Z",
        )

    def abort_incomplete_writes(self, _request: object) -> int:
        raise AssertionError("malformed maintenance request reached the adapter")


def test_asgi_shell_authenticates_before_dispatch() -> None:
    adapter = _Adapter()
    app = create_storage_adapter_app(
        service="fixture-storage-adapter",
        token="secret-token",
        adapter=cast(StorageAdapterPort, adapter),
    )
    client = TestClient(app)

    response = client.get("/v1/adapter")

    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": "unauthorized",
            "message": "Bearer credential is not authorized",
        }
    }
    assert adapter.descriptor_calls == 0


def test_asgi_shell_streams_exact_adapter_responses() -> None:
    adapter = _Adapter()
    app = create_storage_adapter_app(
        service="fixture-storage-adapter",
        token="secret-token",
        adapter=cast(StorageAdapterPort, adapter),
    )
    client = TestClient(app)
    request = ObjectReadRequest(
        object=ObjectLocator(object_path="objects/item"),
        expected_bytes=14,
    )

    response = client.post(
        "/v1/objects/read",
        headers={"Authorization": "Bearer secret-token"},
        content=request.model_dump_json(),
    )

    assert response.status_code == 200
    assert response.headers["content-length"] == "14"
    assert response.content == b"streamed-bytes"


def test_asgi_health_is_public_but_readiness_fails_closed() -> None:
    adapter = _Adapter()

    def unavailable() -> None:
        raise RuntimeError("provider detail")

    app = create_storage_adapter_app(
        service="fixture-storage-adapter",
        token="secret-token",
        adapter=cast(StorageAdapterPort, adapter),
        readiness=unavailable,
    )
    client = TestClient(app)

    assert client.get("/health/live").json() == {
        "service": "fixture-storage-adapter",
        "status": "ok",
    }
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert "provider detail" not in response.text


def test_asgi_shell_rejects_malformed_maintenance_cutoff_before_dispatch() -> None:
    adapter = _Adapter()
    app = create_storage_adapter_app(
        service="fixture-storage-adapter",
        token="secret-token",
        adapter=cast(StorageAdapterPort, adapter),
    )

    response = TestClient(app).post(
        "/v1/maintenance/abort-incomplete-writes",
        headers={"Authorization": "Bearer secret-token"},
        json={"object_prefix": "objects/", "initiated_before": "not-a-timestamp"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_asgi_shell_streams_framed_uploads_without_materializing_request_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _Adapter()
    app = create_storage_adapter_app(
        service="fixture-storage-adapter",
        token="secret-token",
        adapter=cast(StorageAdapterPort, adapter),
    )
    client = TestClient(app)
    payload = b"x" * (3 * 1024 * 1024 + 17)
    declaration = SmallObjectWriteRequest(
        object_path="objects/large-control-object",
        content_type="application/octet-stream",
        required_identity_assertions={"riverhog-format": "fixture/v1"},
        placement="immediate",
        mode="create_only",
        stored_bytes=len(payload),
        stored_sha256=hashlib.sha256(payload).hexdigest(),
    )

    async def forbidden_body(_request: Request) -> bytes:
        raise AssertionError("framed request called Request.body()")

    monkeypatch.setattr(Request, "body", forbidden_body)
    response = client.post(
        "/v1/objects/put",
        headers={
            "Authorization": "Bearer secret-token",
            "Content-Type": FRAMED_REQUEST_MEDIA_TYPE,
            "Content-Length": str(framed_request_length(declaration)),
        },
        content=framed_request(declaration, payload),
    )

    assert response.status_code == 200
    assert b"".join(adapter.upload_chunks) == payload
    assert max(map(len, adapter.upload_chunks)) <= 1024 * 1024


def test_asgi_shell_rejects_unnamed_framing_before_adapter_consumption() -> None:
    adapter = _Adapter()
    app = create_storage_adapter_app(
        service="fixture-storage-adapter",
        token="secret-token",
        adapter=cast(StorageAdapterPort, adapter),
    )
    declaration = SmallObjectWriteRequest(
        object_path="objects/item",
        content_type="application/octet-stream",
        required_identity_assertions={},
        placement="immediate",
        mode="create_only",
        stored_bytes=1,
        stored_sha256=hashlib.sha256(b"x").hexdigest(),
    )
    wire = b"".join(framed_request(declaration, b"x"))

    response = TestClient(app).post(
        "/v1/objects/put",
        headers={"Authorization": "Bearer secret-token"},
        content=wire,
    )

    assert response.status_code == 400
    assert adapter.upload_chunks == []


def test_asgi_shell_authenticates_before_reading_framed_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _Adapter()
    app = create_storage_adapter_app(
        service="fixture-storage-adapter",
        token="secret-token",
        adapter=cast(StorageAdapterPort, adapter),
    )

    async def forbidden_stream(_request: Request) -> AsyncIterator[bytes]:
        raise AssertionError("unauthorized request body was consumed")
        yield  # pragma: no cover - makes this an async iterator

    monkeypatch.setattr(Request, "stream", forbidden_stream)
    response = TestClient(app).post(
        "/v1/objects/put",
        headers={
            "Content-Type": FRAMED_REQUEST_MEDIA_TYPE,
            "Content-Length": "1",
        },
        content=b"x",
    )

    assert response.status_code == 401
    assert adapter.upload_chunks == []

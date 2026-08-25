from __future__ import annotations

from collections.abc import Iterator
from typing import cast

from fastapi.testclient import TestClient
from riverhog_storage_adapter_asgi_support import create_storage_adapter_app
from riverhog_storage_adapter_protocol import (
    AdapterDescriptor,
    ObjectLocator,
    ObjectReadRequest,
    StorageAdapterPort,
)


class _Adapter:
    def __init__(self) -> None:
        self.descriptor_calls = 0

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

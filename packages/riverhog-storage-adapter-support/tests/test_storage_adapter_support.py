from __future__ import annotations

import ast
import hashlib
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from riverhog_storage_adapter_protocol import (
    AbortIncompleteUploadsRequest,
    AdapterDescriptor,
    CompletedObjectReceipt,
    DeleteObjectRequest,
    DeletePrefixRequest,
    ImmutableObjectReceipt,
    MultipartCompleteRequest,
    MultipartCreateRequest,
    MultipartHeadRequest,
    MultipartPartReceipt,
    MultipartUpload,
    ObjectHeadRequest,
    ObjectLocator,
    ObjectMetadataReceipt,
    ObjectReadRequest,
    ReadPreparationRequest,
    ReadStatus,
    SmallObjectWriteRequest,
    StorageAdapterRejection,
)
from riverhog_storage_adapter_support import (
    StorageAdapterClient,
    StorageAdapterConformanceResult,
    StorageAdapterHttpBinding,
    StorageAdapterProtocolError,
    run_storage_adapter_conformance,
    storage_adapter_schema_bundle,
)


class MemoryAdapter:
    def __init__(self) -> None:
        self.created: dict[str, MultipartCreateRequest] = {}
        self.parts: dict[tuple[str, int], bytes] = {}
        self.objects: dict[str, tuple[bytes, dict[str, str], str | None, str]] = {}
        self.small_objects: set[str] = set()
        self.read_requests: list[ReadPreparationRequest] = []

    def descriptor(self) -> AdapterDescriptor:
        return AdapterDescriptor(
            implementation_id="fixture.storage/v1",
            implementation_version="1.0.0",
            read_mode="immediate",
            minimum_nonfinal_part_bytes=1,
            maximum_part_bytes=1024 * 1024,
            maximum_part_count=10_000,
        )

    def create_multipart_upload(self, request: MultipartCreateRequest) -> MultipartUpload:
        upload = MultipartUpload(
            object_path=request.object_path,
            upload_id=f"upload-{len(self.created) + 1}",
        )
        self.created[upload.upload_id] = request
        return upload

    def upload_part(
        self,
        *,
        upload: MultipartUpload,
        number: int,
        content: bytes,
    ) -> MultipartPartReceipt:
        self.parts[(upload.upload_id, number)] = content
        return MultipartPartReceipt(
            number=number,
            part_token=f"part-{number}",
            stored_bytes=len(content),
        )

    def list_parts(self, upload: MultipartUpload) -> tuple[MultipartPartReceipt, ...]:
        return tuple(
            MultipartPartReceipt(
                number=number,
                part_token=f"part-{number}",
                stored_bytes=len(content),
            )
            for (upload_id, number), content in sorted(self.parts.items())
            if upload_id == upload.upload_id
        )

    def complete_multipart_upload(
        self,
        request: MultipartCompleteRequest,
    ) -> CompletedObjectReceipt:
        created = self.created[request.upload.upload_id]
        content = b"".join(
            self.parts[(request.upload.upload_id, part.number)] for part in request.parts
        )
        self.objects[request.upload.object_path] = (
            content,
            created.identity_metadata,
            "version-1",
            created.placement,
        )
        return CompletedObjectReceipt(
            object_path=request.upload.object_path,
            revision="version-1",
            entity_token="completed-token",
            stored_bytes=len(content),
            completed_at="2026-08-21T00:00:00Z",
        )

    def head_completed_object(
        self,
        request: MultipartHeadRequest,
    ) -> CompletedObjectReceipt | None:
        stored = self.objects.get(request.object_path)
        if stored is None:
            return None
        content, metadata, revision, _placement = stored
        if metadata != request.expected_identity_metadata:
            raise RuntimeError("different identity")
        return CompletedObjectReceipt(
            object_path=request.object_path,
            revision=revision,
            entity_token="completed-token",
            stored_bytes=len(content),
            completed_at="2026-08-21T00:00:00Z",
        )

    def abort_multipart_upload(self, upload: MultipartUpload) -> None:
        self.created.pop(upload.upload_id, None)

    def put_small_object(
        self,
        request: SmallObjectWriteRequest,
        content: bytes,
    ) -> ImmutableObjectReceipt:
        existing = self.objects.get(request.object_path)
        if existing is not None and request.mode == "create_only":
            existing_content, existing_metadata, _revision, _placement = existing
            if existing_metadata != request.identity_metadata:
                raise StorageAdapterRejection(
                    "identity_conflict",
                    "object already exists with a different identity",
                )
            return ImmutableObjectReceipt(
                object_path=request.object_path,
                revision="small-version",
                entity_token="small-token",
                stored_bytes=len(existing_content),
                stored_sha256=hashlib.sha256(existing_content).hexdigest(),
                completed_at="2026-08-21T00:00:00Z",
            )
        self.objects[request.object_path] = (
            content,
            request.identity_metadata,
            "small-version",
            request.placement,
        )
        self.small_objects.add(request.object_path)
        return ImmutableObjectReceipt(
            object_path=request.object_path,
            revision="small-version",
            entity_token="small-token",
            stored_bytes=len(content),
            stored_sha256=hashlib.sha256(content).hexdigest(),
            completed_at="2026-08-21T00:00:00Z",
        )

    def head_object(self, request: ObjectHeadRequest) -> ObjectMetadataReceipt | None:
        stored = self.objects.get(request.object.object_path)
        if stored is None:
            return None
        content, metadata, revision, placement = stored
        if placement != request.expected_placement:
            raise RuntimeError("different placement")
        return ObjectMetadataReceipt(
            object_path=request.object.object_path,
            revision=revision,
            entity_token="object-token",
            stored_bytes=len(content),
            stored_sha256=(
                hashlib.sha256(content).hexdigest()
                if request.object.object_path in self.small_objects
                else None
            ),
            identity_metadata=metadata,
            completed_at="2026-08-21T00:00:00Z",
        )

    def iter_object(self, request: ObjectReadRequest) -> Iterator[bytes]:
        content = self.objects[request.object.object_path][0]
        if request.offset is not None and request.size is not None:
            content = content[request.offset : request.offset + request.size]
        yield content

    def delete_object(self, request: DeleteObjectRequest) -> None:
        self.objects.pop(request.object.object_path, None)
        self.small_objects.discard(request.object.object_path)

    def delete_prefix(self, request: DeletePrefixRequest) -> int:
        matched = [path for path in self.objects if path.startswith(request.object_prefix)]
        for path in matched:
            del self.objects[path]
            self.small_objects.discard(path)
        return len(matched)

    def prepare_read(self, request: ReadPreparationRequest) -> ReadStatus:
        self.read_requests.append(request)
        return ReadStatus(state="ready")

    def read_status(self, request: ReadPreparationRequest) -> ReadStatus:
        self.read_requests.append(request)
        return ReadStatus(state="ready")

    def cleanup_read(self, request: ReadPreparationRequest) -> None:
        self.read_requests.append(request)

    def abort_incomplete_uploads(self, request: AbortIncompleteUploadsRequest) -> int:
        _ = request
        return 0


def _client(
    adapter: MemoryAdapter,
    *,
    requests: list[httpx.Request] | None = None,
) -> tuple[StorageAdapterClient, httpx.Client]:
    binding = StorageAdapterHttpBinding(adapter)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer fixture-token"
        if requests is not None:
            requests.append(request)
        result = binding.handle(request.method, request.url.path, request.read())
        content = result.body if isinstance(result.body, bytes) else b"".join(result.body)
        return httpx.Response(
            result.status,
            headers=dict(result.headers),
            content=content,
            request=request,
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return (
        StorageAdapterClient(
            "http://adapter.example.test",
            token="fixture-token",
            allow_insecure_http=True,
            client=http,
        ),
        http,
    )


def test_client_preserves_multipart_unit_receipts_and_declares_body_lengths() -> None:
    adapter = MemoryAdapter()
    requests: list[httpx.Request] = []
    client, http = _client(adapter, requests=requests)
    try:
        created = client.create_multipart_upload(
            MultipartCreateRequest(
                object_path="archives/id/volumes/pack.tar.age",
                content_type="application/octet-stream",
                identity_metadata={"riverhog-format": "riverhog-pack-volume/v1"},
                placement="archive",
            )
        )
        first = client.upload_part(upload=created, number=1, content=b"first")
        second = client.upload_part(upload=created, number=2, content=b"second")

        assert client.list_parts(created) == (first, second)
        completed = client.complete_multipart_upload(
            MultipartCompleteRequest(
                upload=created,
                parts=(first, second),
                expected_bytes=11,
                expected_identity_metadata={"riverhog-format": "riverhog-pack-volume/v1"},
                expected_placement="archive",
            )
        )
        recovered = client.head_completed_object(
            MultipartHeadRequest(
                object_path=created.object_path,
                expected_identity_metadata={"riverhog-format": "riverhog-pack-volume/v1"},
                expected_placement="archive",
            )
        )

        assert completed == recovered
        assert completed.stored_bytes == 11
        assert adapter.objects[created.object_path][0] == b"firstsecond"
        part_requests = [
            request for request in requests if request.url.path == "/v1/multipart/part"
        ]
        assert len(part_requests) == 2
        assert all(
            int(request.headers["Content-Length"]) == len(request.content)
            for request in part_requests
        )
        assert all("Transfer-Encoding" not in request.headers for request in part_requests)
    finally:
        client.close()
        http.close()


def test_small_object_and_exact_range_round_trip() -> None:
    adapter = MemoryAdapter()
    requests: list[httpx.Request] = []
    client, http = _client(adapter, requests=requests)
    content = b"opaque-ciphertext"
    try:
        receipt = client.put_small_object(
            SmallObjectWriteRequest(
                object_path="README.md",
                content_type="text/markdown",
                identity_metadata={"archive-guidance-format": "encrypted-archive-readme-v1"},
                placement="immediate",
                mode="create_only",
                stored_bytes=len(content),
                stored_sha256=hashlib.sha256(content).hexdigest(),
            ),
            content,
        )
        metadata = client.head_object(
            ObjectHeadRequest(
                object=ObjectLocator(object_path="README.md"),
                expected_placement="immediate",
            )
        )
        ranged = b"".join(
            client.iter_object(
                ObjectReadRequest(
                    object=ObjectLocator(
                        object_path="README.md",
                        revision=receipt.revision,
                    ),
                    expected_bytes=len(content),
                    offset=7,
                    size=6,
                )
            )
        )

        assert metadata is not None
        assert metadata.stored_sha256 == receipt.stored_sha256
        assert ranged == content[7:13]
        put_request = next(request for request in requests if request.url.path == "/v1/objects/put")
        assert int(put_request.headers["Content-Length"]) == len(put_request.content)
        assert "Transfer-Encoding" not in put_request.headers
    finally:
        client.close()
        http.close()


def test_read_preparation_sends_no_provider_restore_mechanics() -> None:
    adapter = MemoryAdapter()
    client, http = _client(adapter)
    request = ReadPreparationRequest(
        objects=(ObjectLocator(object_path="archives/id/object", revision="version-1"),)
    )
    try:
        assert client.prepare_read(request).state == "ready"
        assert client.read_status(request).state == "ready"
        client.cleanup_read(request)
        assert adapter.read_requests == [request, request, request]
    finally:
        client.close()
        http.close()


def test_transport_security_requires_explicit_non_loopback_http_opt_in() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        StorageAdapterClient("http://adapter.example.test", token="token")
    client = StorageAdapterClient(
        "http://adapter.example.test",
        token="token",
        allow_insecure_http=True,
        client=httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200))),
    )
    client.close()


def test_binding_closes_errors_without_exposing_provider_exception_text() -> None:
    class FailingAdapter(MemoryAdapter):
        def descriptor(self) -> AdapterDescriptor:
            raise RuntimeError("provider secret detail")

    response = StorageAdapterHttpBinding(FailingAdapter()).handle("GET", "/v1/adapter")
    body = response.body

    assert isinstance(body, bytes)
    assert response.status == 500
    assert b"provider secret detail" not in body
    assert b"internal_failure" in body


def test_binding_enforces_advertised_multipart_limits() -> None:
    class LimitedAdapter(MemoryAdapter):
        def descriptor(self) -> AdapterDescriptor:
            return AdapterDescriptor(
                implementation_id="fixture.storage/v1",
                implementation_version="1.0.0",
                read_mode="immediate",
                minimum_nonfinal_part_bytes=4,
                maximum_part_bytes=5,
                maximum_part_count=2,
            )

    adapter = LimitedAdapter()
    client, http = _client(adapter)
    try:
        upload = client.create_multipart_upload(
            MultipartCreateRequest(
                object_path="archives/id/object",
                content_type="application/octet-stream",
                identity_metadata={"riverhog-format": "fixture/v1"},
                placement="archive",
            )
        )
        with pytest.raises(StorageAdapterProtocolError, match="byte limit"):
            client.upload_part(upload=upload, number=1, content=b"123456")
        with pytest.raises(StorageAdapterProtocolError, match="part number"):
            client.upload_part(upload=upload, number=3, content=b"1234")
    finally:
        client.close()
        http.close()


def test_schema_and_support_source_remain_provider_and_state_neutral() -> None:
    bundle = str(storage_adapter_schema_bundle()).casefold()
    assert "retrieval_tier" not in bundle
    assert "hold_days" not in bundle
    assert "storage_class" not in bundle
    assert "bucket" not in bundle
    assert "cloudfront" not in bundle

    source = Path(__file__).resolve().parents[1] / "src/riverhog_storage_adapter_support"
    imported: set[str] = set()
    for path in source.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.partition(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.partition(".")[0])
    assert imported.isdisjoint(
        {
            "boto3",
            "botocore",
            "fastapi",
            "riverhog_core",
            "sqlalchemy",
            "sqlite3",
        }
    )


def test_client_surfaces_the_closed_adapter_error() -> None:
    binding = StorageAdapterHttpBinding(MemoryAdapter())

    def handler(request: httpx.Request) -> httpx.Response:
        result = binding.handle("POST", "/v1/objects/head", request.read())
        assert isinstance(result.body, bytes)
        return httpx.Response(
            result.status,
            headers=dict(result.headers),
            content=result.body,
            request=request,
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = StorageAdapterClient(
        "http://adapter.example.test",
        token="token",
        allow_insecure_http=True,
        client=http,
    )
    try:
        request = ObjectHeadRequest(
            object=ObjectLocator(object_path="missing"),
            expected_placement="immediate",
        )
        assert client.head_object(request) is None
        with pytest.raises(StorageAdapterProtocolError, match="not found"):
            client._model(  # noqa: SLF001 - verifies the closed wire error
                "POST",
                "/v1/objects/head",
                ObjectMetadataReceipt,
                request,
            )
    finally:
        client.close()
        http.close()


def test_consumer_runnable_conformance_uses_only_the_public_http_contract() -> None:
    adapter = MemoryAdapter()
    client, http = _client(adapter)
    try:
        result = run_storage_adapter_conformance(
            client,
            object_prefix="conformance/run-1",
        )

        assert isinstance(result, StorageAdapterConformanceResult)
        assert result.implementation_id == "fixture.storage/v1"
        assert result.checks == (
            "descriptor",
            "create-only-retry",
            "exact-metadata",
            "exact-range",
            "identity-conflict",
            "multipart-reconciliation",
            "multipart-completion-recovery",
            "multipart-stream",
            "read-preparation",
            "multipart-abort",
            "exact-deletion",
            "version-aware-prefix-cleanup",
        )
        assert not adapter.objects
    finally:
        client.close()
        http.close()

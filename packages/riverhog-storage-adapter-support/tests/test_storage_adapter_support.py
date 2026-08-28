from __future__ import annotations

import ast
import hashlib
import struct
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import GeneratorType

import httpx
import pytest
from riverhog_storage_adapter_protocol import (
    AbortIncompleteWritesRequest,
    AdapterDescriptor,
    CompletedObjectReceipt,
    CompletedWriteLookupRequest,
    DeleteObjectRequest,
    DeletePrefixRequest,
    ImmutableObjectReceipt,
    ObjectHeadRequest,
    ObjectLocator,
    ObjectMetadataReceipt,
    ObjectReadReceipt,
    ObjectReadRequest,
    ObjectReadStream,
    ReadPreparationRequest,
    ReadReady,
    ReadStatus,
    SmallObjectWriteRequest,
    StorageAdapterRejection,
    WriteCompleteRequest,
    WriteSegmentReceipt,
    WriteSegmentRequest,
    WriteSegmentSet,
    WriteSession,
    WriteStartRequest,
)
from riverhog_storage_adapter_support import (
    FRAMED_BODY_FORMAT,
    FRAMED_BODY_MEDIA_TYPE,
    STORAGE_ADAPTER_HTTP_OPERATIONS,
    StorageAdapterClient,
    StorageAdapterConformanceResult,
    StorageAdapterHttpBinding,
    StorageAdapterProtocolError,
    framed_body,
    parse_framed_stream,
    run_storage_adapter_conformance,
    storage_adapter_schema_bundle,
)


@dataclass
class MemoryAdapterState:
    created: dict[str, WriteStartRequest] = field(default_factory=dict)
    segments: dict[tuple[str, int], bytes] = field(default_factory=dict)
    objects: dict[str, tuple[bytes, str, dict[str, str], str | None, str]] = field(
        default_factory=dict
    )
    small_objects: set[str] = field(default_factory=set)
    read_requests: list[ReadPreparationRequest] = field(default_factory=list)


class MemoryAdapter:
    def __init__(self, state: MemoryAdapterState | None = None) -> None:
        shared = state or MemoryAdapterState()
        self.created = shared.created
        self.segments = shared.segments
        self.objects = shared.objects
        self.small_objects = shared.small_objects
        self.read_requests = shared.read_requests

    def descriptor(self) -> AdapterDescriptor:
        return AdapterDescriptor(
            implementation_id="fixture.storage/v1",
            implementation_version="1.0.0",
            read_mode="immediate",
            minimum_nonfinal_segment_bytes=1,
            maximum_segment_bytes=1024 * 1024,
            maximum_segment_count=10_000,
        )

    def begin_write(self, request: WriteStartRequest) -> WriteSession:
        session = WriteSession(
            object_path=request.object_path,
            write_token=f"upload-{len(self.created) + 1}",
        )
        self.created[session.write_token] = request
        return session

    def write_segment(
        self,
        *,
        session: WriteSession,
        number: int,
        stored_bytes: int,
        content: bytes | Iterator[bytes],
    ) -> WriteSegmentReceipt:
        content = content if isinstance(content, bytes) else b"".join(content)
        assert len(content) == stored_bytes
        self.segments[(session.write_token, number)] = content
        return WriteSegmentReceipt(
            number=number,
            segment_token=f"part-{number}",
            stored_bytes=len(content),
        )

    def list_segments(self, session: WriteSession) -> WriteSegmentSet:
        return WriteSegmentSet(
            session=session,
            segments=tuple(
                WriteSegmentReceipt(
                    number=number,
                    segment_token=f"part-{number}",
                    stored_bytes=len(content),
                )
                for (write_token, number), content in sorted(self.segments.items())
                if write_token == session.write_token
            ),
        )

    def complete_write(
        self,
        request: WriteCompleteRequest,
    ) -> CompletedObjectReceipt:
        created = self.created[request.session.write_token]
        content = b"".join(
            self.segments[(request.session.write_token, part.number)] for part in request.segments
        )
        self.objects[request.session.object_path] = (
            content,
            created.content_type,
            created.required_identity_assertions,
            "version-1",
            created.placement,
        )
        return CompletedObjectReceipt(
            object_path=request.session.object_path,
            revision="version-1",
            entity_token="completed-token",
            stored_bytes=len(content),
            verified_content_type=created.content_type,
            verified_identity_assertions=request.required_identity_assertions,
            verified_placement=request.expected_placement,
            completed_at="2026-08-21T00:00:00.000000Z",
        )

    def find_completed_write(
        self,
        request: CompletedWriteLookupRequest,
    ) -> CompletedObjectReceipt | None:
        stored = self.objects.get(request.object_path)
        if stored is None:
            return None
        content, content_type, metadata, revision, placement = stored
        if (
            content_type != request.expected_content_type
            or metadata != request.required_identity_assertions
        ):
            raise RuntimeError("different identity")
        return CompletedObjectReceipt(
            object_path=request.object_path,
            revision=revision,
            entity_token="completed-token",
            stored_bytes=len(content),
            verified_content_type=content_type,
            verified_identity_assertions=metadata,
            verified_placement=placement,
            completed_at="2026-08-21T00:00:00.000000Z",
        )

    def abort_write(self, session: WriteSession) -> None:
        self.created.pop(session.write_token, None)

    def put_small_object(
        self,
        request: SmallObjectWriteRequest,
        content: bytes | Iterator[bytes],
    ) -> ImmutableObjectReceipt:
        content = content if isinstance(content, bytes) else b"".join(content)
        existing = self.objects.get(request.object_path)
        if existing is not None and request.mode == "create_only":
            existing_content, existing_content_type, existing_metadata, _revision, _placement = (
                existing
            )
            if (
                existing_content_type != request.content_type
                or existing_metadata != request.required_identity_assertions
            ):
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
                verified_content_type=existing_content_type,
                verified_identity_assertions=request.required_identity_assertions,
                verified_placement=request.placement,
                completed_at="2026-08-21T00:00:00.000000Z",
            )
        self.objects[request.object_path] = (
            content,
            request.content_type,
            request.required_identity_assertions,
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
            verified_content_type=request.content_type,
            verified_identity_assertions=request.required_identity_assertions,
            verified_placement=request.placement,
            completed_at="2026-08-21T00:00:00.000000Z",
        )

    def head_object(self, request: ObjectHeadRequest) -> ObjectMetadataReceipt | None:
        stored = self.objects.get(request.object.object_path)
        if stored is None:
            return None
        content, content_type, metadata, revision, placement = stored
        if placement != request.expected_placement:
            raise RuntimeError("different placement")
        return ObjectMetadataReceipt(
            object_path=request.object.object_path,
            revision=revision,
            entity_token="object-token",
            content_type=content_type,
            stored_bytes=len(content),
            stored_sha256=(
                hashlib.sha256(content).hexdigest()
                if request.object.object_path in self.small_objects
                else None
            ),
            observed_identity_assertions=metadata,
            verified_placement=request.expected_placement,
            completed_at="2026-08-21T00:00:00.000000Z",
        )

    def read_object(self, request: ObjectReadRequest) -> ObjectReadStream:
        stored = self.objects[request.object.object_path]
        content = stored[0]
        if request.offset is not None and request.size is not None:
            content = content[request.offset : request.offset + request.size]
        return ObjectReadStream(
            receipt=ObjectReadReceipt(
                object=ObjectLocator(
                    object_path=request.object.object_path,
                    revision=stored[3],
                ),
                total_bytes=request.expected_bytes,
                offset=request.offset or 0,
                read_bytes=len(content),
            ),
            content=iter((content,)) if content else iter(()),
        )

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
        return ReadStatus(objects=request.objects, readiness=ReadReady())

    def read_status(self, request: ReadPreparationRequest) -> ReadStatus:
        self.read_requests.append(request)
        return ReadStatus(objects=request.objects, readiness=ReadReady())

    def cleanup_read(self, request: ReadPreparationRequest) -> None:
        self.read_requests.append(request)

    def abort_incomplete_writes(self, request: AbortIncompleteWritesRequest) -> int:
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


def test_client_preserves_write_segment_receipts_and_declares_body_lengths() -> None:
    adapter = MemoryAdapter()
    requests: list[httpx.Request] = []
    client, http = _client(adapter, requests=requests)
    try:
        created = client.begin_write(
            WriteStartRequest(
                object_path="archives/id/volumes/pack.tar.age",
                content_type="application/octet-stream",
                required_identity_assertions={"riverhog-format": "riverhog-pack-volume/v1"},
                placement="archive",
            )
        )
        first = client.write_segment(session=created, number=1, stored_bytes=5, content=b"first")
        second = client.write_segment(session=created, number=2, stored_bytes=6, content=b"second")

        assert client.list_segments(created).segments == (first, second)
        completed = client.complete_write(
            WriteCompleteRequest(
                session=created,
                segments=(first, second),
                expected_bytes=11,
                expected_content_type="application/octet-stream",
                required_identity_assertions={"riverhog-format": "riverhog-pack-volume/v1"},
                expected_placement="archive",
            )
        )
        recovered = client.find_completed_write(
            CompletedWriteLookupRequest(
                object_path=created.object_path,
                expected_content_type="application/octet-stream",
                required_identity_assertions={"riverhog-format": "riverhog-pack-volume/v1"},
                expected_placement="archive",
            )
        )

        assert completed == recovered
        assert completed.stored_bytes == 11
        assert adapter.objects[created.object_path][0] == b"firstsecond"
        segment_requests = [
            request for request in requests if request.url.path == "/v1/writes/segment"
        ]
        assert len(segment_requests) == 2
        assert all(
            int(request.headers["Content-Length"]) == len(request.content)
            for request in segment_requests
        )
        assert all("Transfer-Encoding" not in request.headers for request in segment_requests)
        assert all(
            request.headers["Content-Type"] == FRAMED_BODY_MEDIA_TYPE
            for request in segment_requests
        )
    finally:
        client.close()
        http.close()


def test_named_framing_parses_split_declaration_and_streams_exact_payload() -> None:
    request = WriteSegmentRequest(
        session=WriteSession(object_path="archives/id/object", write_token="upload-1"),
        number=1,
        stored_bytes=9,
    )
    wire = b"".join(framed_body(request, (b"opa", b"que", b"123")))
    chunks = (wire[:1], wire[1:4], wire[4:17], wire[17:-2], wire[-2:])

    declaration, content = parse_framed_stream(
        chunks,
        WriteSegmentRequest,
        content_length=len(wire),
    )

    assert FRAMED_BODY_FORMAT == "riverhog-json-opaque-framing/v1"
    assert declaration == request
    assert b"".join(content) == b"opaque123"
    content.require_consumed()


def test_named_framing_rejects_a_body_length_different_from_its_declaration() -> None:
    request = WriteSegmentRequest(
        session=WriteSession(object_path="archives/id/object", write_token="upload-1"),
        number=1,
        stored_bytes=3,
    )
    wire = b"".join(framed_body(request, b"abc"))

    with pytest.raises(ValueError, match="content length"):
        parse_framed_stream(
            (wire,),
            WriteSegmentRequest,
            content_length=len(wire) + 1,
        )


def test_named_framing_rejects_truncated_and_trailing_content() -> None:
    request = WriteSegmentRequest(
        session=WriteSession(object_path="archives/id/object", write_token="upload-1"),
        number=1,
        stored_bytes=3,
    )
    wire = b"".join(framed_body(request, b"abc"))

    with pytest.raises(ValueError, match="declaration is truncated"):
        parse_framed_stream(
            (wire[:2],),
            WriteSegmentRequest,
            content_length=len(wire),
        )
    _, truncated = parse_framed_stream(
        (wire[:-1],),
        WriteSegmentRequest,
        content_length=len(wire),
    )
    with pytest.raises(ValueError, match="ended before"):
        b"".join(truncated)
    _, trailing = parse_framed_stream(
        (wire, b"trailing"),
        WriteSegmentRequest,
        content_length=len(wire),
    )
    with pytest.raises(ValueError, match="exceeds"):
        b"".join(trailing)


def test_named_framing_rejects_an_oversized_declaration_before_reading_it() -> None:
    oversized = 32 * 1024 + 1

    with pytest.raises(ValueError, match="declaration length"):
        parse_framed_stream(
            (struct.pack(">I", oversized),),
            WriteSegmentRequest,
            content_length=4 + oversized,
        )


def test_framed_binding_requires_length_and_reports_trailing_input_as_invalid() -> None:
    adapter = MemoryAdapter()
    binding = StorageAdapterHttpBinding(adapter)
    request = WriteSegmentRequest(
        session=WriteSession(object_path="archives/id/object", write_token="upload-1"),
        number=1,
        stored_bytes=3,
    )
    wire = b"".join(framed_body(request, b"abc"))

    missing = binding.handle_framed(
        "POST",
        "/v1/writes/segment",
        (wire,),
        content_length=None,
    )
    trailing = binding.handle_framed(
        "POST",
        "/v1/writes/segment",
        (wire, b"trailing"),
        content_length=len(wire),
    )

    assert missing.status == 411
    assert trailing.status == 400


def test_client_streams_declared_segment_chunks_without_transfer_encoding() -> None:
    adapter = MemoryAdapter()
    requests: list[httpx.Request] = []
    client, http = _client(adapter, requests=requests)
    try:
        session = client.begin_write(
            WriteStartRequest(
                object_path="archives/id/object",
                content_type="application/octet-stream",
                required_identity_assertions={"riverhog-format": "fixture/v1"},
                placement="archive",
            )
        )
        receipt = client.write_segment(
            session=session,
            number=1,
            stored_bytes=9,
            content=(chunk for chunk in (b"opa", b"que", b"123")),
        )

        request = next(current for current in requests if current.url.path == "/v1/writes/segment")
        assert receipt.stored_bytes == 9
        assert adapter.segments[(session.write_token, 1)] == b"opaque123"
        assert int(request.headers["Content-Length"]) == len(request.content)
        assert "Transfer-Encoding" not in request.headers
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
                required_identity_assertions={
                    "archive-guidance-format": "encrypted-archive-readme-v1"
                },
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
            client.read_object(
                ObjectReadRequest(
                    object=ObjectLocator(
                        object_path="README.md",
                        revision=receipt.revision,
                    ),
                    expected_bytes=len(content),
                    offset=7,
                    size=6,
                )
            ).content
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
        assert client.prepare_read(request).readiness.state == "ready"
        assert client.read_status(request).readiness.state == "ready"
        client.cleanup_read(request)
        assert adapter.read_requests == [request, request, request]
    finally:
        client.close()
        http.close()


def test_binding_closes_provider_read_when_consumer_stops_after_receipt() -> None:
    class ClosingAdapter(MemoryAdapter):
        closed = False

        def read_object(self, request: ObjectReadRequest) -> ObjectReadStream:
            return ObjectReadStream(
                receipt=ObjectReadReceipt(
                    object=request.object,
                    total_bytes=request.expected_bytes,
                    offset=0,
                    read_bytes=request.expected_bytes,
                ),
                content=iter((b"payload",)),
                close=lambda: setattr(self, "closed", True),
            )

    adapter = ClosingAdapter()
    request = ObjectReadRequest(
        object=ObjectLocator(object_path="archives/id/object", revision="version-1"),
        expected_bytes=7,
    )
    response = StorageAdapterHttpBinding(adapter).handle(
        "POST",
        "/v1/objects/read",
        request.model_dump_json(exclude_none=True).encode(),
    )
    assert isinstance(response.body, GeneratorType)
    assert next(response.body)
    response.body.close()

    assert adapter.closed is True


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


def test_adapter_implementation_value_error_is_a_server_fault() -> None:
    class FaultingAdapter(MemoryAdapter):
        def begin_write(
            self,
            _request: WriteStartRequest,
        ) -> WriteSession:
            raise ValueError("private adapter defect")

    request = WriteStartRequest(
        object_path="archives/id/object",
        content_type="application/octet-stream",
        required_identity_assertions={"riverhog-format": "fixture/v1"},
        placement="archive",
    )
    response = StorageAdapterHttpBinding(FaultingAdapter()).handle(
        "POST",
        "/v1/writes/begin",
        request.model_dump_json(exclude_none=True).encode(),
    )

    assert response.status == 500
    assert isinstance(response.body, bytes)
    assert b'"code":"internal_failure"' in response.body
    assert b"private adapter defect" not in response.body


def test_binding_enforces_advertised_write_segment_limits() -> None:
    class LimitedAdapter(MemoryAdapter):
        def descriptor(self) -> AdapterDescriptor:
            return AdapterDescriptor(
                implementation_id="fixture.storage/v1",
                implementation_version="1.0.0",
                read_mode="immediate",
                minimum_nonfinal_segment_bytes=4,
                maximum_segment_bytes=5,
                maximum_segment_count=2,
            )

    adapter = LimitedAdapter()
    client, http = _client(adapter)
    try:
        upload = client.begin_write(
            WriteStartRequest(
                object_path="archives/id/object",
                content_type="application/octet-stream",
                required_identity_assertions={"riverhog-format": "fixture/v1"},
                placement="archive",
            )
        )
        with pytest.raises(StorageAdapterProtocolError, match="byte limit"):
            client.write_segment(session=upload, number=1, stored_bytes=6, content=b"123456")
        with pytest.raises(StorageAdapterProtocolError, match="segment number"):
            client.write_segment(session=upload, number=3, stored_bytes=4, content=b"1234")
    finally:
        client.close()
        http.close()


def test_schema_and_support_source_remain_provider_and_state_neutral() -> None:
    document = storage_adapter_schema_bundle()
    bundle = str(document).casefold()
    assert "retrieval_tier" not in bundle
    assert "hold_days" not in bundle
    assert "storage_class" not in bundle
    assert "bucket" not in bundle
    assert "cloudfront" not in bundle

    operations = document["http_binding"]["operations"]
    assert [(item["method"], item["path"]) for item in operations] == [
        (operation.method, operation.path) for operation in STORAGE_ADAPTER_HTTP_OPERATIONS
    ]
    segment_set = next(item for item in operations if item["path"] == "/v1/writes/segments")
    assert segment_set["response"] == {
        "kind": "json",
        "schema": "WriteSegmentSet",
        "statuses": [200],
        "headers": [],
    }
    read = next(item for item in operations if item["path"] == "/v1/objects/read")
    assert read["response"]["kind"] == "framed"
    assert read["response"]["schema"] == "ObjectReadReceipt"
    assert [header["name"] for header in read["response"]["headers"]] == [
        "Content-Length",
        "X-Riverhog-Object-Bytes",
        "X-Riverhog-Object-Revision",
        "Content-Range",
    ]
    assert all(header["schema"] == {"type": "string"} for header in read["response"]["headers"])
    assert "WriteSegmentSet" in document["schemas"]

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
    state = MemoryAdapterState()
    adapter = MemoryAdapter(state)
    restarted_adapter = MemoryAdapter(state)
    client, http = _client(adapter)
    continuation_client, continuation_http = _client(restarted_adapter)
    try:
        result = run_storage_adapter_conformance(
            client,
            continuation_client=continuation_client,
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
            "sparse-write-reconciliation",
            "write-continuation-replay",
            "write-reconciliation",
            "write-completion-recovery",
            "write-stream",
            "read-preparation",
            "write-abort",
            "exact-deletion",
            "version-aware-prefix-cleanup",
        )
        assert not adapter.objects
    finally:
        continuation_client.close()
        continuation_http.close()
        client.close()
        http.close()

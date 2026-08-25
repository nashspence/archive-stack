from __future__ import annotations

import ast
import hashlib
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError
from riverhog_storage_adapter_protocol import (
    AbortIncompleteWritesRequest,
    CompletedWriteLookupRequest,
    DeleteObjectRequest,
    DeletePrefixRequest,
    ObjectHeadRequest,
    ObjectLocator,
    ObjectReadRequest,
    ReadPreparationRequest,
    ReadReadiness,
    ReadReady,
    ReadRequested,
    SmallObjectWriteRequest,
    StorageAdapterRejection,
    WriteCompleteRequest,
    WriteStartRequest,
)
from riverhog_storage_adapter_s3_support import (
    S3ClientConfig,
    S3StorageAdapter,
    S3StorageAdapterConfig,
    S3TransportTuning,
    create_s3_client,
)


def _client_error(code: str, status: int, operation: str) -> ClientError:
    return ClientError(
        cast(
            Any,
            {
                "Error": {"Code": code, "Message": code},
                "ResponseMetadata": {"HTTPStatusCode": status},
            },
        ),
        operation,
    )


class _Body(BytesIO):
    def iter_chunks(self, *, chunk_size: int) -> Any:
        while chunk := self.read(chunk_size):
            yield chunk


def _read_request_body(body: object) -> bytes:
    if isinstance(body, bytes):
        return body
    read = getattr(body, "read", None)
    if not callable(read):
        raise TypeError("request body is not readable")
    chunks: list[bytes] = []
    while chunk := read(64 * 1024):
        chunks.append(bytes(chunk))
    return b"".join(chunks)


class _Paginator:
    def __init__(self, client: _FakeS3Client, operation: str) -> None:
        self.client = client
        self.operation = operation

    def paginate(self, *, Bucket: str, Prefix: str) -> tuple[dict[str, object], ...]:
        assert Bucket == "fixture-bucket"
        if self.operation == "list_objects_v2":
            return (
                {
                    "Contents": [
                        {"Key": key}
                        for key in sorted(self.client.current)
                        if key.startswith(Prefix)
                    ]
                },
            )
        if self.operation == "list_object_versions":
            return (
                {
                    "Versions": [
                        {"Key": key, "VersionId": version}
                        for (key, version) in sorted(self.client.versions)
                        if key.startswith(Prefix)
                    ],
                    "DeleteMarkers": [],
                },
            )
        raise AssertionError(self.operation)


class _FakeS3Client:
    def __init__(self) -> None:
        self.current: dict[str, str] = {}
        self.versions: dict[tuple[str, str], dict[str, Any]] = {}
        self.uploads: dict[str, dict[str, Any]] = {}
        self.next_upload = 1
        self.next_version = 1
        self.get_attempts = 0
        self.deleted: list[dict[str, object]] = []

    def _store(self, request: dict[str, Any], content: bytes) -> dict[str, str]:
        key = str(request["Key"])
        version = f"v{self.next_version}"
        self.next_version += 1
        value = {
            "Body": content,
            "ContentLength": len(content),
            "ContentType": request.get("ContentType"),
            "Metadata": dict(request.get("Metadata") or {}),
            "StorageClass": request.get("StorageClass"),
            "ETag": f'"etag-{version}"',
            "LastModified": datetime(2026, 1, 1, tzinfo=UTC),
            "VersionId": version,
        }
        self.versions[(key, version)] = value
        self.current[key] = version
        return {"VersionId": version}

    def create_multipart_upload(self, **request: Any) -> dict[str, str]:
        upload_id = f"upload-{self.next_upload}"
        self.next_upload += 1
        self.uploads[upload_id] = {
            "request": request,
            "parts": {},
            "Initiated": datetime(2026, 1, 1, tzinfo=UTC),
        }
        return {"UploadId": upload_id}

    def upload_part(self, **request: Any) -> dict[str, str]:
        upload = self.uploads[str(request["UploadId"])]
        number = int(request["PartNumber"])
        content = _read_request_body(request["Body"])
        assert len(content) == int(request["ContentLength"])
        upload["parts"][number] = content
        return {"ETag": f'"part-{number}"'}

    def list_parts(self, **request: Any) -> dict[str, object]:
        parts = self.uploads[str(request["UploadId"])]["parts"]
        return {
            "IsTruncated": False,
            "Parts": [
                {"PartNumber": number, "ETag": f'"part-{number}"', "Size": len(content)}
                for number, content in sorted(parts.items())
            ],
        }

    def complete_multipart_upload(self, **request: Any) -> dict[str, str]:
        key = str(request["Key"])
        if request.get("IfNoneMatch") == "*" and key in self.current:
            raise _client_error("PreconditionFailed", 412, "CompleteWriteSession")
        upload_id = str(request["UploadId"])
        if upload_id not in self.uploads:
            raise _client_error("NoSuchUpload", 404, "CompleteWriteSession")
        upload = self.uploads.pop(upload_id)
        content = b"".join(upload["parts"][number] for number in sorted(upload["parts"]))
        return self._store(upload["request"], content)

    def abort_multipart_upload(self, **request: Any) -> None:
        self.uploads.pop(str(request["UploadId"]), None)

    def list_multipart_uploads(self, **request: Any) -> dict[str, object]:
        prefix = str(request["Prefix"])
        return {
            "IsTruncated": False,
            "Uploads": [
                {
                    "Key": str(upload["request"]["Key"]),
                    "UploadId": upload_id,
                    "Initiated": upload["Initiated"],
                }
                for upload_id, upload in sorted(self.uploads.items())
                if str(upload["request"]["Key"]).startswith(prefix)
            ],
        }

    def head_object(self, **request: Any) -> dict[str, object]:
        key = str(request["Key"])
        version = str(request.get("VersionId") or self.current.get(key, ""))
        value = self.versions.get((key, version))
        if value is None:
            raise _client_error("NoSuchKey", 404, "HeadObject")
        return {name: current for name, current in value.items() if name != "Body"}

    def get_object(self, **request: Any) -> dict[str, object]:
        self.get_attempts += 1
        key = str(request["Key"])
        version = str(request.get("VersionId") or self.current[key])
        content = bytes(self.versions[(key, version)]["Body"])
        if "Range" not in request:
            return {"Body": _Body(content), "ContentLength": len(content)}
        start, end = (
            int(value) for value in str(request["Range"]).removeprefix("bytes=").split("-")
        )
        selected = content[start : end + 1]
        return {
            "Body": _Body(selected),
            "ContentLength": len(selected),
            "ContentRange": f"bytes {start}-{end}/{len(content)}",
        }

    def delete_object(self, **request: Any) -> None:
        key = str(request["Key"])
        if request.get("VersionId") is not None:
            version = str(request["VersionId"])
            self.versions.pop((key, version), None)
            if self.current.get(key) == version:
                self.current.pop(key, None)
        else:
            self.current.pop(key, None)

    def delete_objects(self, **request: Any) -> None:
        for item in request["Delete"]["Objects"]:
            current = dict(item)
            self.deleted.append(current)
            key = str(current["Key"])
            if current.get("VersionId") is None:
                self.current.pop(key, None)
            else:
                version = str(current["VersionId"])
                self.versions.pop((key, version), None)
                if self.current.get(key) == version:
                    self.current.pop(key, None)

    def get_paginator(self, operation: str) -> _Paginator:
        return _Paginator(self, operation)


def _config(**overrides: Any) -> S3StorageAdapterConfig:
    values: dict[str, Any] = {
        "implementation_id": "fixture.s3/v1",
        "implementation_version": "1.0.0",
        "bucket": "fixture-bucket",
        "root_prefix": "owned",
        "archive_storage_class": "DEEP_ARCHIVE",
    }
    values.update(overrides)
    return S3StorageAdapterConfig(**values)


def _small_request(
    content: bytes,
    *,
    identity: str = "logical/v1",
) -> SmallObjectWriteRequest:
    return SmallObjectWriteRequest(
        object_path="archives/collection/manifest.age",
        content_type="application/octet-stream",
        required_identity_assertions={"riverhog-logical-identity": identity},
        placement="immediate",
        mode="create_only",
        stored_bytes=len(content),
        stored_sha256=hashlib.sha256(content).hexdigest(),
    )


def test_small_object_retry_uses_exact_identity_without_rereading_ciphertext() -> None:
    client = _FakeS3Client()
    adapter = S3StorageAdapter(client, _config())
    first_content = b"first randomized ciphertext"

    first = adapter.put_small_object(_small_request(first_content), first_content)
    second = adapter.put_small_object(_small_request(first_content), first_content)

    assert second == first
    assert client.get_attempts == 0
    stored = client.versions[("owned/archives/collection/manifest.age", first.revision or "")]
    assert stored["Body"] == first_content
    assert first.stored_sha256 == hashlib.sha256(first_content).hexdigest()


def test_small_object_rejects_a_changed_logical_identity() -> None:
    client = _FakeS3Client()
    adapter = S3StorageAdapter(client, _config())
    adapter.put_small_object(_small_request(b"first"), b"first")

    with pytest.raises(StorageAdapterRejection) as raised:
        adapter.put_small_object(
            _small_request(b"second", identity="different/v1"),
            b"second",
        )

    assert raised.value.code == "identity_conflict"


def test_small_object_streams_once_before_atomic_conditional_publication() -> None:
    client = _FakeS3Client()
    adapter = S3StorageAdapter(client, _config())
    consumed: list[bytes] = []

    def content() -> Any:
        for chunk in (b"cipher", b"text"):
            consumed.append(chunk)
            yield chunk

    receipt = adapter.put_small_object(_small_request(b"ciphertext"), content())

    assert receipt.stored_bytes == 10
    assert consumed == [b"cipher", b"text"]
    assert client.get_attempts == 0
    assert not client.uploads


def test_resumable_write_reconciles_segments_and_lost_completion() -> None:
    client = _FakeS3Client()
    adapter = S3StorageAdapter(client, _config())
    create = WriteStartRequest(
        object_path="archives/collection/volume.age",
        content_type="application/octet-stream",
        required_identity_assertions={"riverhog-plan-sha256": "a" * 64},
        placement="archive",
    )
    session = adapter.begin_write(create)
    first_content = b"f" * adapter.descriptor().minimum_nonfinal_segment_bytes
    segments = (
        adapter.write_segment(
            session=session,
            number=1,
            stored_bytes=len(first_content),
            content=first_content,
        ),
        adapter.write_segment(
            session=session,
            number=2,
            stored_bytes=6,
            content=b"second",
        ),
    )
    assert adapter.list_segments(session).segments == segments
    completion = WriteCompleteRequest(
        session=session,
        segments=segments,
        expected_bytes=len(first_content) + 6,
        required_identity_assertions=create.required_identity_assertions,
        expected_placement=create.placement,
    )

    first = adapter.complete_write(completion)
    recovered = adapter.complete_write(completion)

    assert recovered == first
    assert not hasattr(first, "stored_sha256")
    assert (
        adapter.find_completed_write(
            CompletedWriteLookupRequest(
                object_path=first.object_path,
                required_identity_assertions=create.required_identity_assertions,
                expected_placement=create.placement,
            )
        )
        == first
    )
    head = client.versions[("owned/archives/collection/volume.age", first.revision or "")]
    assert head["StorageClass"] == "DEEP_ARCHIVE"
    assert head["Metadata"] == {
        "riverhog-adapter-placement": "archive",
        "riverhog-plan-sha256": "a" * 64,
    }


def test_full_and_exact_range_reads_preserve_version_and_length() -> None:
    client = _FakeS3Client()
    adapter = S3StorageAdapter(client, _config())
    content = b"0123456789"
    receipt = adapter.put_small_object(_small_request(content), content)
    locator = ObjectLocator(
        object_path="archives/collection/manifest.age",
        revision=receipt.revision,
    )

    assert (
        b"".join(
            adapter.iter_object(ObjectReadRequest(object=locator, expected_bytes=len(content)))
        )
        == content
    )
    assert (
        b"".join(
            adapter.iter_object(
                ObjectReadRequest(
                    object=locator,
                    expected_bytes=len(content),
                    offset=3,
                    size=4,
                )
            )
        )
        == b"3456"
    )


def test_metadata_head_hides_adapter_markers_but_keeps_opaque_identity() -> None:
    client = _FakeS3Client()
    adapter = S3StorageAdapter(client, _config())
    content = b"ciphertext"
    receipt = adapter.put_small_object(_small_request(content), content)

    head = adapter.head_object(
        ObjectHeadRequest(
            object=ObjectLocator(
                object_path="archives/collection/manifest.age",
                revision=receipt.revision,
            ),
            expected_placement="immediate",
        )
    )

    assert head is not None
    assert head.required_identity_assertions == {"riverhog-logical-identity": "logical/v1"}
    assert head.stored_sha256 == hashlib.sha256(content).hexdigest()


def test_deletion_is_exact_and_prefix_cleanup_removes_all_versions() -> None:
    client = _FakeS3Client()
    adapter = S3StorageAdapter(client, _config())
    first = adapter.put_small_object(_small_request(b"first"), b"first")
    adapter.put_small_object(
        _small_request(b"second", identity="logical/v2").model_copy(
            update={"mode": "replace_current"}
        ),
        b"second",
    )

    adapter.delete_object(
        DeleteObjectRequest(
            object=ObjectLocator(
                object_path="archives/collection/manifest.age",
                revision=first.revision,
            ),
            mode="exact_revision",
        )
    )
    assert ("owned/archives/collection/manifest.age", first.revision or "") not in client.versions

    affected = adapter.delete_prefix(DeletePrefixRequest(object_prefix="archives/collection/"))

    assert affected >= 1
    assert not client.current
    assert not client.versions
    assert all(item["Key"] == "owned/archives/collection/manifest.age" for item in client.deleted)


def test_read_preparation_mechanics_remain_adapter_private() -> None:
    class Preparation:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[tuple[str, str | None], ...]]] = []

        def prepare(self, **kwargs: Any) -> ReadReadiness:
            self.calls.append(("prepare", kwargs["objects"]))
            return ReadRequested(estimated_ready_at="2026-01-03T00:00:00Z")

        def status(self, **kwargs: Any) -> ReadReadiness:
            self.calls.append(("status", kwargs["objects"]))
            return ReadReady(available_until="2026-01-04T00:00:00Z")

        def cleanup(self, **kwargs: Any) -> None:
            self.calls.append(("cleanup", kwargs["objects"]))

    preparation = Preparation()
    adapter = S3StorageAdapter(
        _FakeS3Client(),
        _config(read_mode="restore_required"),
        read_preparation=preparation,
    )
    request = ReadPreparationRequest(
        objects=(ObjectLocator(object_path="archives/collection/volume.age", revision="v1"),)
    )

    assert adapter.prepare_read(request).readiness.state == "requested"
    assert adapter.read_status(request).readiness.state == "ready"
    adapter.cleanup_read(request)

    assert preparation.calls == [
        ("prepare", (("owned/archives/collection/volume.age", "v1"),)),
        ("status", (("owned/archives/collection/volume.age", "v1"),)),
        ("cleanup", (("owned/archives/collection/volume.age", "v1"),)),
    ]
    schema = str(ReadPreparationRequest.model_json_schema()).casefold()
    assert "tier" not in schema
    assert "hold" not in schema
    assert "storage_class" not in schema


def test_incomplete_upload_cleanup_is_prefix_and_cutoff_scoped() -> None:
    client = _FakeS3Client()
    adapter = S3StorageAdapter(client, _config())
    session = adapter.begin_write(
        WriteStartRequest(
            object_path="archives/collection/volume.age",
            content_type="application/octet-stream",
            required_identity_assertions={"riverhog-plan-sha256": "a" * 64},
            placement="archive",
        )
    )

    aborted = adapter.abort_incomplete_writes(
        AbortIncompleteWritesRequest(
            object_prefix="archives/",
            initiated_before="2026-01-02T00:00:00Z",
        )
    )

    assert aborted == 1
    assert session.write_token not in client.uploads


def test_s3_transport_and_source_boundary_are_adapter_owned() -> None:
    tuning = S3TransportTuning(max_pool_connections=64, max_attempts=9)
    assert tuning.max_pool_connections == 64

    source = Path(__file__).resolve().parents[1] / "src/riverhog_storage_adapter_s3_support"
    imported: set[str] = set()
    for path in source.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module)
    assert all(not name.startswith("riverhog_core") for name in imported)
    assert all(not name.startswith("riverhog_storage_adapter_support") for name in imported)


def test_s3_client_preserves_one_pass_request_bodies() -> None:
    client = create_s3_client(
        S3ClientConfig(
            endpoint_url="https://s3.example.test",
            region="example-1",
            access_key_id="example-access-key",
            secret_access_key="example-secret-key",
        )
    )

    assert client.meta.config.request_checksum_calculation == "when_required"
    assert client.meta.config.s3["payload_signing_enabled"] is False

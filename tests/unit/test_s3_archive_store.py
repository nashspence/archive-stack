from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from riverhog_core.collection_archives import (
    CollectionArchiveFile,
    CollectionArchivePackage,
    build_collection_archive_package,
)
from riverhog_core.ports.archive_store import (
    ArchiveMultipartUploadedPart,
    ArchiveMultipartUploadState,
    ArchiveMultipartUploadTracker,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.stores.s3_archive_store import (
    COLLECTION_BYTES_METADATA,
    COLLECTION_SHA256_METADATA,
    S3ArchiveStore,
)
from tests.fixtures.crypto import FixtureProofStamper
from tests.fixtures.data import DOCS_FILES


class _MissingObjectError(Exception):
    def __init__(self) -> None:
        self.response = {"Error": {"Code": "404"}}


class _FakeBody:
    def __init__(self, content: bytes) -> None:
        self._content = content
        self.closed = False

    def iter_chunks(self, *, chunk_size: int) -> Iterator[bytes]:
        for offset in range(0, len(self._content), chunk_size):
            yield self._content[offset : offset + chunk_size]

    def read(self) -> bytes:
        return self._content

    def close(self) -> None:
        self.closed = True


class _FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.uploads: dict[str, dict[str, Any]] = {}
        self.put_object_keys: list[str] = []
        self.uploaded_part_sizes: list[int] = []
        self.aborted_uploads: list[str] = []
        self.completed_uploads: list[str] = []
        self.restore_requests: list[str] = []
        self._next_upload_id = 1
        self.fail_next_upload_part_after_successes: int | None = None
        self.successful_upload_part_calls = 0

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        _ = Bucket
        try:
            return {key: value for key, value in self.objects[Key].items() if key != "Body"}
        except KeyError as exc:
            raise _MissingObjectError() from exc

    def put_object(self, *, Bucket: str, Key: str, Body: object, **kwargs: Any) -> None:
        _ = Bucket
        self.put_object_keys.append(Key)
        if isinstance(Body, bytes):
            body = Body
        else:
            read = Body.read
            body = cast(bytes, read())
        self.objects[Key] = {
            "Body": body,
            "ContentLength": len(body),
            "LastModified": datetime(2026, 4, 20, 4, 1, 0, tzinfo=UTC),
            **kwargs,
        }

    def create_multipart_upload(self, *, Bucket: str, Key: str, **kwargs: Any) -> dict[str, str]:
        _ = Bucket
        upload_id = f"upload-{self._next_upload_id}"
        self._next_upload_id += 1
        self.uploads[upload_id] = {"Key": Key, "Parts": {}, "ExtraArgs": kwargs}
        return {"UploadId": upload_id}

    def upload_part(
        self,
        *,
        Bucket: str,
        Key: str,
        UploadId: str,
        PartNumber: int,
        Body: bytes,
    ) -> dict[str, str]:
        _ = Bucket
        upload = self.uploads[UploadId]
        assert upload["Key"] == Key
        if (
            self.fail_next_upload_part_after_successes is not None
            and self.successful_upload_part_calls >= self.fail_next_upload_part_after_successes
        ):
            self.fail_next_upload_part_after_successes = None
            raise RuntimeError("synthetic upload_part failure")
        upload["Parts"][PartNumber] = Body
        self.successful_upload_part_calls += 1
        self.uploaded_part_sizes.append(len(Body))
        return {"ETag": f"etag-{UploadId}-{PartNumber}"}

    def list_parts(
        self,
        *,
        Bucket: str,
        Key: str,
        UploadId: str,
        PartNumberMarker: int = 0,
    ) -> dict[str, object]:
        _ = Bucket
        upload = self.uploads[UploadId]
        assert upload["Key"] == Key
        parts = [
            {
                "PartNumber": part_number,
                "ETag": f"etag-{UploadId}-{part_number}",
                "Size": len(body),
            }
            for part_number, body in sorted(upload["Parts"].items())
            if part_number > PartNumberMarker
        ]
        return {"IsTruncated": False, "Parts": parts}

    def complete_multipart_upload(
        self,
        *,
        Bucket: str,
        Key: str,
        UploadId: str,
        MultipartUpload: dict[str, list[dict[str, object]]],
    ) -> None:
        _ = Bucket
        upload = self.uploads.pop(UploadId)
        assert upload["Key"] == Key
        body = b"".join(upload["Parts"][part["PartNumber"]] for part in MultipartUpload["Parts"])
        self.objects[Key] = {
            "Body": body,
            "ContentLength": len(body),
            "LastModified": datetime(2026, 4, 20, 4, 1, 0, tzinfo=UTC),
            **upload["ExtraArgs"],
        }
        self.completed_uploads.append(UploadId)

    def abort_multipart_upload(self, *, Bucket: str, Key: str, UploadId: str) -> None:
        _ = Bucket, Key
        self.aborted_uploads.append(UploadId)
        self.uploads.pop(UploadId, None)

    def restore_object(
        self,
        *,
        Bucket: str,
        Key: str,
        RestoreRequest: dict[str, object],
    ) -> None:
        _ = Bucket, RestoreRequest
        self.restore_requests.append(Key)
        self.objects[Key]["Restore"] = 'ongoing-request="true"'

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        _ = Bucket
        return {"Body": _FakeBody(cast(bytes, self.objects[Key]["Body"]))}


class _FakeMultipartTracker(ArchiveMultipartUploadTracker):
    def __init__(self) -> None:
        self.state: ArchiveMultipartUploadState | None = None
        self.progress: list[tuple[int, int, int]] = []
        self.parts: list[ArchiveMultipartUploadedPart] = []
        self.cleared: list[str] = []

    def load_multipart_upload(
        self,
        *,
        collection_id: str,
        object_path: str,
        part_size: int,
        content_length: int,
        sha256: str,
    ) -> ArchiveMultipartUploadState | None:
        _ = collection_id
        if self.state is None:
            return None
        if self.state.object_path != object_path:
            return None
        if self.state.part_size != part_size:
            return None
        if self.state.content_length != content_length:
            return None
        if self.state.sha256 != sha256:
            return None
        return replace(self.state, parts=tuple(self.parts))

    def save_multipart_upload(
        self,
        *,
        collection_id: str,
        state: ArchiveMultipartUploadState,
    ) -> None:
        _ = collection_id
        self.state = state
        self.parts = list(state.parts)

    def record_multipart_upload_progress(
        self,
        *,
        collection_id: str,
        state: ArchiveMultipartUploadState,
        part: ArchiveMultipartUploadedPart,
        uploaded_bytes: int,
        uploaded_parts: int,
        total_parts: int,
    ) -> None:
        _ = collection_id, state
        self.parts = [current for current in self.parts if current.part_number != part.part_number]
        self.parts.append(part)
        self.parts.sort(key=lambda current: current.part_number)
        self.progress.append((uploaded_bytes, uploaded_parts, total_parts))

    def clear_multipart_upload(
        self,
        *,
        collection_id: str,
        upload_id: str,
    ) -> None:
        _ = collection_id
        self.cleared.append(upload_id)
        self.state = None
        self.parts = []


def _config(tmp_path: Path, **overrides: object) -> RuntimeConfig:
    config = RuntimeConfig(
        object_store="s3",
        s3_endpoint_url="http://example.invalid:9000",
        s3_region="us-east-1",
        s3_bucket="riverhog",
        s3_access_key_id="test-access",
        s3_secret_access_key="test-secret",
        s3_force_path_style=True,
        tusd_base_url="http://example.invalid:1080/files",
        tusd_hook_secret="hook-secret",
        sqlite_path=tmp_path / "state.sqlite3",
    )
    return replace(config, **overrides)


def _package() -> CollectionArchivePackage:
    return build_collection_archive_package(
        collection_id="docs",
        files=tuple(
            CollectionArchiveFile(
                path=path,
                content=content,
                sha256=hashlib.sha256(content).hexdigest(),
            )
            for path, content in sorted(DOCS_FILES.items())
        ),
        stamper=FixtureProofStamper(),
    )


def _store_with_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    client: _FakeS3Client,
    **config_overrides: object,
) -> S3ArchiveStore:
    monkeypatch.setattr(
        "riverhog_core.stores.s3_archive_store.create_glacier_s3_client",
        lambda config: client,
    )
    return S3ArchiveStore(_config(tmp_path, **config_overrides))


def test_upload_collection_archive_package_uploads_collection_manifest_and_proof_objects(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    store = _store_with_client(
        monkeypatch,
        tmp_path,
        client,
        glacier_backend="aws",
        glacier_endpoint_url="https://s3.us-west-2.amazonaws.com",
        glacier_storage_class="DEEP_ARCHIVE",
    )
    package = _package()

    receipt = store.upload_collection_archive_package(collection_id="docs", package=package)

    assert receipt.archive.object_path.endswith("/archive.tar")
    assert receipt.archive.object_path == "glacier/collections/docs/archive.tar"
    assert receipt.manifest.object_path == "glacier/collections/docs/manifest.yml"
    assert receipt.proof.object_path == "glacier/collections/docs/manifest.yml.ots"
    assert receipt.manifest.stored_bytes == len(package.manifest_bytes)
    assert receipt.proof.stored_bytes == len(package.proof_bytes)
    assert receipt.archive.storage_class == "DEEP_ARCHIVE"
    assert receipt.manifest.storage_class == "STANDARD"
    assert receipt.proof.storage_class == "STANDARD"
    assert receipt.archive_format == "tar"
    assert receipt.compression == "none"
    archive_head = client.objects[receipt.archive.object_path]
    manifest_head = client.objects[receipt.manifest.object_path]
    proof_head = client.objects[receipt.proof.object_path]
    assert set(client.objects) == {
        receipt.archive.object_path,
        receipt.manifest.object_path,
        receipt.proof.object_path,
    }
    assert manifest_head["Body"] == package.manifest_bytes
    assert proof_head["Body"] == package.proof_bytes
    assert archive_head["StorageClass"] == "DEEP_ARCHIVE"
    assert "StorageClass" not in manifest_head
    assert "StorageClass" not in proof_head
    archive_metadata = archive_head["Metadata"]
    assert archive_metadata[COLLECTION_BYTES_METADATA] == str(len(package.archive_bytes))
    assert archive_metadata[COLLECTION_SHA256_METADATA] == package.archive_sha256
    assert archive_metadata["riverhog-archive-format"] == "tar"
    assert archive_metadata["riverhog-compression"] == "none"
    assert archive_metadata["riverhog-storage-class"] == "DEEP_ARCHIVE"
    assert manifest_head["Metadata"]["riverhog-storage-class"] == "STANDARD"
    assert proof_head["Metadata"]["riverhog-storage-class"] == "STANDARD"


def test_upload_collection_archive_package_streams_archive_with_multipart(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    store = _store_with_client(
        monkeypatch,
        tmp_path,
        client,
        glacier_multipart_part_bytes=4,
        glacier_multipart_concurrency=1,
    )
    monkeypatch.setattr("riverhog_core.stores.s3_archive_store._MIN_MULTIPART_PART_SIZE", 4)
    package = _package()

    receipt = store.upload_collection_archive_package(collection_id="docs", package=package)

    archive_head = client.objects[receipt.archive.object_path]
    assert archive_head["Body"] == package.archive_bytes
    assert receipt.archive.object_path not in client.put_object_keys
    assert client.completed_uploads == ["upload-1"]
    assert client.aborted_uploads == []
    assert client.uploads == {}
    assert client.uploaded_part_sizes[:-1]
    assert all(size == 4 for size in client.uploaded_part_sizes[:-1])
    assert archive_head["Metadata"][COLLECTION_SHA256_METADATA] == package.archive_sha256


def test_upload_collection_archive_package_tracks_parallel_multipart_progress(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    store = _store_with_client(
        monkeypatch,
        tmp_path,
        client,
        glacier_multipart_part_bytes=4,
        glacier_multipart_concurrency=2,
    )
    tracker = _FakeMultipartTracker()
    monkeypatch.setattr("riverhog_core.stores.s3_archive_store._MIN_MULTIPART_PART_SIZE", 4)
    package = _package()

    receipt = store.upload_collection_archive_package(
        collection_id="docs",
        package=package,
        multipart_tracker=tracker,
    )

    expected_parts = (len(package.archive_bytes) + 3) // 4
    assert client.objects[receipt.archive.object_path]["Body"] == package.archive_bytes
    assert client.completed_uploads == ["upload-1"]
    assert tracker.progress[-1] == (len(package.archive_bytes), expected_parts, expected_parts)
    assert tracker.cleared == ["upload-1"]


def test_upload_collection_archive_package_resumes_existing_multipart_upload(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    store = _store_with_client(
        monkeypatch,
        tmp_path,
        client,
        glacier_multipart_part_bytes=4,
        glacier_multipart_concurrency=1,
    )
    tracker = _FakeMultipartTracker()
    monkeypatch.setattr("riverhog_core.stores.s3_archive_store._MIN_MULTIPART_PART_SIZE", 4)
    package = _package()
    client.fail_next_upload_part_after_successes = 1

    with pytest.raises(RuntimeError, match="synthetic upload_part failure"):
        store.upload_collection_archive_package(
            collection_id="docs",
            package=package,
            multipart_tracker=tracker,
        )

    assert tracker.state is not None
    assert tracker.progress == [(4, 1, (len(package.archive_bytes) + 3) // 4)]
    assert [(part.part_number, part.etag, part.size) for part in tracker.parts] == [
        (1, "etag-upload-1-1", 4)
    ]
    assert client.aborted_uploads == []
    first_upload_id = tracker.state.upload_id
    stored_first_part = client.uploads[first_upload_id]["Parts"][1]

    resumed_offsets: list[int] = []

    def resume_archive(offset: int) -> Iterator[bytes]:
        resumed_offsets.append(offset)
        yield package.archive_bytes[offset:]

    seekable_package = replace(package, _archive_chunks_from_offset=resume_archive)
    receipt = store.upload_collection_archive_package(
        collection_id="docs",
        package=seekable_package,
        multipart_tracker=tracker,
    )

    assert receipt.archive.object_path == "glacier/collections/docs/archive.tar"
    assert client.completed_uploads == [first_upload_id]
    assert client.aborted_uploads == []
    assert tracker.state is None
    assert tracker.cleared == [first_upload_id]
    assert resumed_offsets == [4]
    assert client.uploads == {}
    assert client.objects[receipt.archive.object_path]["Body"] == package.archive_bytes
    assert client.uploaded_part_sizes[0] == 4
    assert stored_first_part == package.archive_bytes[:4]


def test_request_collection_archive_restore_requests_collection_manifest_and_proof(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    store = _store_with_client(
        monkeypatch,
        tmp_path,
        client,
        glacier_backend="aws",
        glacier_endpoint_url="https://s3.us-west-2.amazonaws.com",
        glacier_storage_class="DEEP_ARCHIVE",
    )
    package = _package()
    receipt = store.upload_collection_archive_package(collection_id="docs", package=package)

    status = store.request_collection_archive_restore(
        collection_id="docs",
        object_path=receipt.archive.object_path,
        manifest_object_path=receipt.manifest.object_path,
        proof_object_path=receipt.proof.object_path,
        retrieval_tier="bulk",
        hold_days=1,
        requested_at="2026-04-20T04:00:00Z",
        estimated_ready_at="2026-04-22T04:00:00Z",
    )

    assert status.state == "requested"
    assert client.restore_requests == [receipt.archive.object_path]


def test_intelligent_tiering_archive_access_is_not_treated_as_ready(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    store = _store_with_client(
        monkeypatch,
        tmp_path,
        client,
        glacier_backend="aws",
        glacier_endpoint_url="https://s3.us-west-2.amazonaws.com",
        glacier_storage_class="INTELLIGENT_TIERING",
    )
    package = _package()
    receipt = store.upload_collection_archive_package(collection_id="docs", package=package)
    client.objects[receipt.archive.object_path]["ArchiveStatus"] = "ARCHIVE_ACCESS"

    status = store.get_collection_archive_restore_status(
        collection_id="docs",
        object_path=receipt.archive.object_path,
        requested_at="2026-04-20T04:00:00Z",
        estimated_ready_at="2026-04-20T16:00:00Z",
        estimated_expires_at=None,
    )

    assert status.state == "requested"


def test_iter_restored_collection_archive_streams_when_ready(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    store = _store_with_client(monkeypatch, tmp_path, client)
    package = _package()
    receipt = store.upload_collection_archive_package(collection_id="docs", package=package)

    chunks = list(
        store.iter_restored_collection_archive(
            collection_id="docs",
            object_path=receipt.archive.object_path,
        )
    )

    assert b"".join(chunks) == package.archive_bytes

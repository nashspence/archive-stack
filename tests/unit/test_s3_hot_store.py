from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError

from riverhog_core.ports.archive_store import (
    ArchiveMultipartUploadedPart,
    ArchiveMultipartUploadState,
)
from riverhog_core.ports.hot_store import HotCollectionFile, HotCollectionListing
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.stores.s3_hot_store import S3HotStore, _multipart_part_size
from tests.unit.db_helpers import sqlite_url


class _FakeBody:
    def __init__(self, content: bytes) -> None:
        self._content = content

    def read(self) -> bytes:
        return self._content


class _FakeS3Client:
    class exceptions:
        ClientError = ClientError

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.uploads: dict[str, dict[str, Any]] = {}
        self.uploaded_part_sizes: list[int] = []
        self.aborted_uploads: list[str] = []
        self.completed_uploads: list[str] = []
        self._next_upload_id = 1

    def put_object(self, *, Bucket: str, Key: str, Body: object, **kwargs: Any) -> None:
        _ = Bucket
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
        self.uploads[upload_id] = {"Key": Key, "Parts": {}, **kwargs}
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
        upload["Parts"][PartNumber] = Body
        self.uploaded_part_sizes.append(len(Body))
        return {"ETag": f"etag-{UploadId}-{PartNumber}"}

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
            "Metadata": upload.get("Metadata", {}),
        }
        self.completed_uploads.append(UploadId)

    def list_parts(self, **request: object) -> dict[str, object]:
        upload = self.uploads[str(request["UploadId"])]
        parts = [
            {
                "PartNumber": number,
                "ETag": f"etag-{request['UploadId']}-{number}",
                "Size": len(body),
            }
            for number, body in sorted(upload["Parts"].items())
        ]
        return {"Parts": parts, "IsTruncated": False}

    def abort_multipart_upload(self, *, Bucket: str, Key: str, UploadId: str) -> None:
        _ = Bucket, Key
        self.aborted_uploads.append(UploadId)
        self.uploads.pop(UploadId, None)

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        _ = Bucket
        self.objects.pop(Key, None)

    def get_paginator(self, operation_name: str) -> _FakeListObjectsV2Paginator:
        assert operation_name == "list_objects_v2"
        return _FakeListObjectsV2Paginator(self)

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        _ = Bucket
        return {"Body": _FakeBody(cast(bytes, self.objects[Key]["Body"]))}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        _ = Bucket
        try:
            return {
                "ContentLength": self.objects[Key]["ContentLength"],
                "LastModified": self.objects[Key]["LastModified"],
                "Metadata": self.objects[Key].get("Metadata", {}),
            }
        except KeyError as exc:
            raise ClientError(
                {
                    "Error": {"Code": "NoSuchKey"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "HeadObject",
            ) from exc


class _FakeListObjectsV2Paginator:
    def __init__(self, client: _FakeS3Client) -> None:
        self._client = client

    def paginate(self, *, Bucket: str, Prefix: str) -> Iterator[dict[str, object]]:
        _ = Bucket
        entries = [
            {"Key": key, "Size": details["ContentLength"]}
            for key, details in reversed(self._client.objects.items())
            if key.startswith(Prefix)
        ]
        split = len(entries) // 2
        yield {"Contents": entries[:split]}
        yield {"Contents": entries[split:]}


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
        database_url=sqlite_url(tmp_path / "state.sqlite3"),
        **overrides,
    )
    return config


def _store_with_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    client: _FakeS3Client,
    **config_overrides: object,
) -> S3HotStore:
    monkeypatch.setattr(
        "riverhog_core.stores.s3_hot_store.create_s3_client",
        lambda config: client,
    )
    return S3HotStore(_config(tmp_path, **config_overrides))


def test_put_collection_file_stream_uses_single_put_for_small_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    store = _store_with_client(monkeypatch, tmp_path, client)

    store.put_collection_file_stream(
        "2025/20250102T030405Z__docs",
        "small.bin",
        (chunk for chunk in [b"abc", b"defg", b"hi"]),
        content_length=9,
        sha256="19cc02f26df43cc571bc9ed7b0c4d29224a3ec229529221725ef76d021c8326f",
    )

    assert store.get_collection_file("2025/20250102T030405Z__docs", "small.bin") == b"abcdefghi"
    assert client.uploaded_part_sizes == []
    assert client.completed_uploads == []
    assert client.uploads == {}


def test_collection_listing_reports_deterministic_storage_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    store = _store_with_client(monkeypatch, tmp_path, client)
    collection_id = "2025/20250102T030405Z__docs"

    store.put_collection_file(collection_id, "zeta.txt", b"last")
    store.put_collection_file(collection_id, "alpha.txt", b"first")
    store.put_collection_file(collection_id, "active-upload.part", b"temporary")
    store.put_collection_file(collection_id, "active-upload.info", b"temporary")
    store.put_collection_file("2025/20250103T030405Z__other", "other.txt", b"other")

    assert store.list_collection_files(collection_id) == HotCollectionListing(
        files=(
            HotCollectionFile(path="alpha.txt", bytes=5),
            HotCollectionFile(path="zeta.txt", bytes=4),
        ),
        file_count=2,
        total_bytes=9,
    )


def test_put_collection_file_stream_completes_multipart_upload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    store = _store_with_client(monkeypatch, tmp_path, client, hot_single_put_max_bytes=0)
    monkeypatch.setattr("riverhog_core.stores.s3_hot_store._MIN_MULTIPART_PART_SIZE", 4)

    store.put_collection_file_stream(
        "2025/20250102T030405Z__docs",
        "large.bin",
        (chunk for chunk in [b"abc", b"defg", b"hi"]),
        content_length=9,
        sha256="19cc02f26df43cc571bc9ed7b0c4d29224a3ec229529221725ef76d021c8326f",
    )

    assert store.get_collection_file("2025/20250102T030405Z__docs", "large.bin") == b"abcdefghi"
    assert store.stat_collection_file("2025/20250102T030405Z__docs", "large.bin").bytes == 9
    assert (
        store.stat_collection_file("2025/20250102T030405Z__docs", "large.bin").sha256
        == "19cc02f26df43cc571bc9ed7b0c4d29224a3ec229529221725ef76d021c8326f"
    )
    assert client.uploaded_part_sizes == [4, 4, 1]
    assert client.completed_uploads == ["upload-1"]
    assert client.aborted_uploads == []
    assert client.uploads == {}


def test_put_collection_file_stream_aborts_multipart_upload_after_failed_stream(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    store = _store_with_client(monkeypatch, tmp_path, client, hot_single_put_max_bytes=0)
    monkeypatch.setattr("riverhog_core.stores.s3_hot_store._MIN_MULTIPART_PART_SIZE", 3)

    def chunks() -> Iterable[bytes]:
        yield b"abc"
        raise ValueError("bad stream")

    with pytest.raises(ValueError, match="bad stream"):
        store.put_collection_file_stream(
            "2025/20250102T030405Z__docs",
            "large.bin",
            chunks(),
            content_length=9,
        )

    assert "collections/docs/large.bin" not in client.objects
    assert client.uploaded_part_sizes == [3]
    assert client.aborted_uploads == ["upload-1"]
    assert client.completed_uploads == []
    assert client.uploads == {}


class _MemoryMultipartTracker:
    def __init__(self) -> None:
        self.state: ArchiveMultipartUploadState | None = None
        self.cleared_upload_ids: list[str] = []

    def load_multipart_upload(
        self,
        *,
        collection_id: str,
        object_path: str,
        part_size: int,
        content_length: int,
        sha256: str,
    ) -> ArchiveMultipartUploadState | None:
        _ = collection_id, object_path, part_size, content_length, sha256
        return self.state

    def save_multipart_upload(
        self,
        *,
        collection_id: str,
        state: ArchiveMultipartUploadState,
    ) -> None:
        _ = collection_id
        self.state = state

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
        _ = collection_id, uploaded_bytes, uploaded_parts, total_parts
        self.state = ArchiveMultipartUploadState(
            upload_id=state.upload_id,
            object_path=state.object_path,
            part_size=state.part_size,
            content_length=state.content_length,
            sha256=state.sha256,
            parts=(*state.parts, part),
        )

    def clear_multipart_upload(self, *, collection_id: str, upload_id: str) -> None:
        _ = collection_id
        self.cleared_upload_ids.append(upload_id)
        self.state = None


def test_put_collection_file_stream_resumes_tracked_multipart_upload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    store = _store_with_client(monkeypatch, tmp_path, client, hot_single_put_max_bytes=0)
    tracker = _MemoryMultipartTracker()
    monkeypatch.setattr("riverhog_core.stores.s3_hot_store._MIN_MULTIPART_PART_SIZE", 4)

    def interrupted_chunks() -> Iterable[bytes]:
        yield b"abcd"
        raise ValueError("network disappeared")

    with pytest.raises(ValueError, match="network disappeared"):
        store.put_collection_file_stream_resumable(
            "2025/20250102T030405Z__docs",
            "large.bin",
            interrupted_chunks(),
            content_length=9,
            sha256="19cc02f26df43cc571bc9ed7b0c4d29224a3ec229529221725ef76d021c8326f",
            multipart_tracker=tracker,
        )

    assert client.aborted_uploads == []
    assert tracker.state is not None
    assert len(tracker.state.parts) == 1

    store.put_collection_file_stream_resumable(
        "2025/20250102T030405Z__docs",
        "large.bin",
        (chunk for chunk in [b"abc", b"defg", b"hi"]),
        content_length=9,
        sha256="19cc02f26df43cc571bc9ed7b0c4d29224a3ec229529221725ef76d021c8326f",
        multipart_tracker=tracker,
    )

    assert store.get_collection_file("2025/20250102T030405Z__docs", "large.bin") == b"abcdefghi"
    assert client.uploaded_part_sizes == [4, 4, 1]
    assert client.completed_uploads == ["upload-1"]
    assert tracker.cleared_upload_ids == ["upload-1"]
    assert tracker.state is None


def test_multipart_part_size_scales_to_s3_part_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("riverhog_core.stores.s3_hot_store._MIN_MULTIPART_PART_SIZE", 4)
    monkeypatch.setattr("riverhog_core.stores.s3_hot_store._MAX_MULTIPART_PARTS", 3)

    assert _multipart_part_size(13) == 5

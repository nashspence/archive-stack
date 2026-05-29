from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Any, cast

import pytest

from riverhog_core.ports.archive_store import (
    ArchiveMultipartUploadedPart,
    ArchiveMultipartUploadState,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.stores.s3_protection_mirror_store import S3ProtectionMirrorStore


class _FakeBody:
    def __init__(self, content: bytes) -> None:
        self._content = content

    def read(self) -> bytes:
        return self._content


class _FakePaginator:
    def __init__(self, client: _FakeS3Client) -> None:
        self._client = client

    def paginate(self, *, Bucket: str, Prefix: str) -> Iterable[dict[str, object]]:
        _ = Bucket
        yield {
            "Contents": [
                {"Key": key}
                for key in sorted(self._client.objects)
                if key.startswith(Prefix)
            ]
        }


class _FakeS3Client:
    class exceptions:
        ClientError = Exception

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.uploads: dict[str, dict[str, Any]] = {}
        self.uploaded_part_sizes: list[int] = []
        self.completed_uploads: list[str] = []
        self.deleted_objects: list[str] = []
        self._next_upload_id = 1

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **kwargs: Any) -> None:
        _ = Bucket
        self.objects[Key] = {
            "Body": Body,
            "ContentLength": len(Body),
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

    def list_parts(self, **request: object) -> dict[str, object]:
        upload = self.uploads[str(request["UploadId"])]
        return {
            "Parts": [
                {
                    "PartNumber": number,
                    "ETag": f"etag-{request['UploadId']}-{number}",
                    "Size": len(body),
                }
                for number, body in sorted(upload["Parts"].items())
            ],
            "IsTruncated": False,
        }

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
        parts = cast(dict[int, bytes], upload["Parts"])
        body = b"".join(
            parts[int(cast(str | int, part["PartNumber"]))]
            for part in MultipartUpload["Parts"]
        )
        self.objects[Key] = {
            "Body": body,
            "ContentLength": len(body),
            "Metadata": upload.get("Metadata", {}),
        }
        self.completed_uploads.append(UploadId)

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        _ = Bucket
        return {
            "ContentLength": self.objects[Key]["ContentLength"],
            "Metadata": self.objects[Key].get("Metadata", {}),
        }

    def get_object(self, **request: object) -> dict[str, object]:
        key = str(request["Key"])
        content = cast(bytes, self.objects[key]["Body"])
        range_header = request.get("Range")
        if range_header is not None:
            start_text, end_text = str(range_header).removeprefix("bytes=").split("-", 1)
            start = int(start_text)
            end = len(content) - 1 if not end_text else int(end_text)
            content = content[start : end + 1]
        return {"Body": _FakeBody(content)}

    def get_paginator(self, name: str) -> _FakePaginator:
        assert name == "list_objects_v2"
        return _FakePaginator(self)

    def delete_objects(self, *, Bucket: str, Delete: dict[str, object]) -> None:
        _ = Bucket
        for item in cast(list[dict[str, object]], Delete["Objects"]):
            key = str(item["Key"])
            self.deleted_objects.append(key)
            self.objects.pop(key, None)


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


def _config() -> RuntimeConfig:
    return RuntimeConfig(
        object_store="s3",
        s3_endpoint_url="http://example.invalid:9000",
        s3_region="us-east-1",
        s3_bucket="riverhog",
        s3_access_key_id="test-access",
        s3_secret_access_key="test-secret",
        s3_force_path_style=True,
        tusd_base_url="http://example.invalid:1080/files",
        tusd_hook_secret="hook-secret",
        protection_mirror_enabled=True,
        protection_mirror_s3_endpoint_url="https://s3.example.invalid",
        protection_mirror_s3_bucket="mirror",
        protection_mirror_s3_access_key_id="mirror-key",
        protection_mirror_s3_secret_access_key="mirror-secret",
        protection_mirror_prefix="riverhog/protection-mirror",
        protection_mirror_multipart_part_bytes=4,
    )


def test_protection_mirror_archive_upload_resumes_multipart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeS3Client()
    monkeypatch.setattr(
        "riverhog_core.stores.s3_protection_mirror_store.create_protection_mirror_s3_client",
        lambda config: client,
    )
    monkeypatch.setattr("riverhog_core.stores.s3_hot_store._MIN_MULTIPART_PART_SIZE", 4)
    store = S3ProtectionMirrorStore(_config())
    tracker = _MemoryMultipartTracker()
    content = b"abcdefghi"
    sha256 = hashlib.sha256(content).hexdigest()

    def interrupted_chunks() -> Iterable[bytes]:
        yield b"abcd"
        raise ValueError("network disappeared")

    with pytest.raises(ValueError, match="network disappeared"):
        store.put_collection_archive_stream_resumable(
            "20250712T213200Z__photos",
            interrupted_chunks(),
            content_length=len(content),
            sha256=sha256,
            multipart_tracker=tracker,
        )

    assert tracker.state is not None
    assert len(tracker.state.parts) == 1

    store.put_collection_archive_stream_resumable(
        "20250712T213200Z__photos",
        (chunk for chunk in [b"abc", b"defg", b"hi"]),
        content_length=len(content),
        sha256=sha256,
        multipart_tracker=tracker,
    )

    stat = store.stat_collection_archive("20250712T213200Z__photos")
    assert stat is not None
    assert stat.bytes == len(content)
    assert stat.sha256 == sha256
    assert b"".join(store.iter_collection_archive("20250712T213200Z__photos")) == content
    assert client.uploaded_part_sizes == [4, 4, 1]
    assert client.completed_uploads == ["upload-1"]
    assert tracker.cleared_upload_ids == ["upload-1"]
    assert tracker.state is None


def test_protection_mirror_delete_removes_collection_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeS3Client()
    monkeypatch.setattr(
        "riverhog_core.stores.s3_protection_mirror_store.create_protection_mirror_s3_client",
        lambda config: client,
    )
    store = S3ProtectionMirrorStore(_config())
    client.objects[store.object_path("20250712T213200Z__photos")] = {
        "Body": b"content",
        "ContentLength": 7,
    }

    store.delete_collection("20250712T213200Z__photos")

    assert store.object_path("20250712T213200Z__photos") in client.deleted_objects

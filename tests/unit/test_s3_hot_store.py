from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.stores.s3_hot_store import S3HotStore, _multipart_part_size


class _FakeBody:
    def __init__(self, content: bytes) -> None:
        self._content = content

    def read(self) -> bytes:
        return self._content


class _FakeS3Client:
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

    def create_multipart_upload(self, *, Bucket: str, Key: str) -> dict[str, str]:
        _ = Bucket
        upload_id = f"upload-{self._next_upload_id}"
        self._next_upload_id += 1
        self.uploads[upload_id] = {"Key": Key, "Parts": {}}
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
        body = b"".join(
            upload["Parts"][part["PartNumber"]]
            for part in MultipartUpload["Parts"]
        )
        self.objects[Key] = {
            "Body": body,
            "ContentLength": len(body),
            "LastModified": datetime(2026, 4, 20, 4, 1, 0, tzinfo=UTC),
        }
        self.completed_uploads.append(UploadId)

    def abort_multipart_upload(self, *, Bucket: str, Key: str, UploadId: str) -> None:
        _ = Bucket, Key
        self.aborted_uploads.append(UploadId)
        self.uploads.pop(UploadId, None)

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        _ = Bucket
        self.objects.pop(Key, None)

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        _ = Bucket
        return {"Body": _FakeBody(cast(bytes, self.objects[Key]["Body"]))}


def _config(tmp_path: Path) -> RuntimeConfig:
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
    return config


def _store_with_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    client: _FakeS3Client,
) -> S3HotStore:
    monkeypatch.setattr(
        "riverhog_core.stores.s3_hot_store.create_s3_client",
        lambda config: client,
    )
    return S3HotStore(_config(tmp_path))


def test_put_collection_file_stream_completes_multipart_upload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    store = _store_with_client(monkeypatch, tmp_path, client)
    monkeypatch.setattr("riverhog_core.stores.s3_hot_store._MIN_MULTIPART_PART_SIZE", 4)

    store.put_collection_file_stream(
        "docs",
        "large.bin",
        (chunk for chunk in [b"abc", b"defg", b"hi"]),
        content_length=9,
    )

    assert store.get_collection_file("docs", "large.bin") == b"abcdefghi"
    assert client.uploaded_part_sizes == [4, 4, 1]
    assert client.completed_uploads == ["upload-1"]
    assert client.aborted_uploads == []
    assert client.uploads == {}


def test_put_collection_file_stream_aborts_multipart_upload_after_failed_stream(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    store = _store_with_client(monkeypatch, tmp_path, client)
    monkeypatch.setattr("riverhog_core.stores.s3_hot_store._MIN_MULTIPART_PART_SIZE", 3)

    def chunks() -> Iterable[bytes]:
        yield b"abc"
        raise ValueError("bad stream")

    with pytest.raises(ValueError, match="bad stream"):
        store.put_collection_file_stream(
            "docs",
            "large.bin",
            chunks(),
            content_length=9,
        )

    assert "collections/docs/large.bin" not in client.objects
    assert client.uploaded_part_sizes == [3]
    assert client.aborted_uploads == ["upload-1"]
    assert client.completed_uploads == []
    assert client.uploads == {}


def test_multipart_part_size_scales_to_s3_part_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("riverhog_core.stores.s3_hot_store._MIN_MULTIPART_PART_SIZE", 4)
    monkeypatch.setattr("riverhog_core.stores.s3_hot_store._MAX_MULTIPART_PARTS", 3)

    assert _multipart_part_size(13) == 5

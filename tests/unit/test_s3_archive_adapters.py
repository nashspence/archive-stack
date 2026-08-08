from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError
from riverhog_core.ports.archive_ingress_store import ArchiveObjectIdentityConflict
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.stores.s3_archive_ingress_store import S3ArchiveMultipartObjectStore
from riverhog_core.stores.s3_archive_manifest_store import S3ImmutableArchiveObjectStore
from riverhog_core.stores.s3_archive_range_store import S3ArchiveObjectRangeStore

from tests.unit.db_helpers import sqlite_url


def _client_error(code: str, status: int, operation: str) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        operation,
    )


class _Body(BytesIO):
    def close(self) -> None:
        super().close()


class _FakeClient:
    def __init__(self, *, conditional_put_supported: bool = True) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.uploads: dict[str, dict[str, Any]] = {}
        self.next_upload = 1
        self.conditional_put_supported = conditional_put_supported
        self.put_attempts = 0

    def create_multipart_upload(self, **request: Any) -> dict[str, str]:
        upload_id = f"upload-{self.next_upload}"
        self.next_upload += 1
        self.uploads[upload_id] = {"request": request, "parts": {}}
        return {"UploadId": upload_id}

    def upload_part(self, **request: Any) -> dict[str, str]:
        upload = self.uploads[str(request["UploadId"])]
        number = int(request["PartNumber"])
        upload["parts"][number] = bytes(request["Body"])
        return {"ETag": f'"etag-{number}"'}

    def list_parts(self, **request: Any) -> dict[str, object]:
        parts = self.uploads[str(request["UploadId"])]["parts"]
        return {
            "IsTruncated": False,
            "Parts": [
                {"PartNumber": number, "ETag": f'"etag-{number}"', "Size": len(content)}
                for number, content in sorted(parts.items())
            ],
        }

    def complete_multipart_upload(self, **request: Any) -> dict[str, object]:
        key = str(request["Key"])
        if request.get("IfNoneMatch") != "*":
            raise AssertionError("multipart completion must be create-only")
        if key in self.objects:
            raise _client_error("PreconditionFailed", 412, "CompleteMultipartUpload")
        upload = self.uploads.pop(str(request["UploadId"]))
        body = b"".join(upload["parts"][number] for number in sorted(upload["parts"]))
        created = upload["request"]
        self.objects[key] = {
            "Body": body,
            "ContentLength": len(body),
            "Metadata": dict(created["Metadata"]),
            "ETag": '"complete"',
            "LastModified": datetime(2026, 1, 1, tzinfo=UTC),
        }
        return {}

    def abort_multipart_upload(self, **request: Any) -> None:
        self.uploads.pop(str(request["UploadId"]), None)

    def put_object(self, **request: Any) -> dict[str, object]:
        self.put_attempts += 1
        key = str(request["Key"])
        if request.get("IfNoneMatch") != "*":
            raise AssertionError("immutable put must be create-only")
        if not self.conditional_put_supported:
            raise _client_error("NotImplemented", 501, "PutObject")
        if key in self.objects:
            raise _client_error("PreconditionFailed", 412, "PutObject")
        body = bytes(request["Body"])
        self.objects[key] = {
            "Body": body,
            "ContentLength": len(body),
            "Metadata": dict(request["Metadata"]),
            "ETag": '"put"',
            "LastModified": datetime(2026, 1, 1, tzinfo=UTC),
        }
        return {}

    def head_object(self, **request: Any) -> dict[str, object]:
        key = str(request["Key"])
        if key not in self.objects:
            raise _client_error("NoSuchKey", 404, "HeadObject")
        return {key: value for key, value in self.objects[key].items() if key != "Body"}

    def get_object(self, **request: Any) -> dict[str, object]:
        content = bytes(self.objects[str(request["Key"])]["Body"])
        range_header = str(request["Range"])
        start, end = (int(value) for value in range_header.removeprefix("bytes=").split("-"))
        selected = content[start : end + 1]
        return {
            "Body": _Body(selected),
            "ContentLength": len(selected),
            "ContentRange": f"bytes {start}-{end}/{len(content)}",
        }


def _config(tmp_path: Path) -> RuntimeConfig:
    config = RuntimeConfig(database_url=sqlite_url(tmp_path / "state.sqlite3"))
    store = replace(config.archive_store("archive"), name="primary", storage_class="STANDARD")
    return replace(
        config,
        archive_stores={"primary": store},
        archive_write_store="primary",
        archive_read_order=("primary",),
    )


def test_multipart_adapter_completes_create_only_and_recovers_the_same_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeClient()
    monkeypatch.setattr(
        "riverhog_core.stores.s3_archive_ingress_store.create_archive_s3_client",
        lambda *_args, **_kwargs: client,
    )
    config = _config(tmp_path)
    store = S3ArchiveMultipartObjectStore(config, config.archive_store("primary"))
    metadata = {"riverhog-plan-sha256": "a" * 64}
    upload = store.create_multipart_upload(
        object_path="archive/volumes/pack-000000000000.tar.age",
        content_type="application/vnd.riverhog.pack-volume+age",
        metadata=metadata,
    )
    parts = (
        store.upload_part(upload=upload, number=1, content=b"first"),
        store.upload_part(upload=upload, number=2, content=b"second"),
    )

    receipt = store.complete_multipart_upload(
        upload=upload,
        parts=parts,
        expected_bytes=11,
        expected_metadata=metadata,
    )
    assert receipt.bytes == 11
    assert (
        store.head_completed_object(
            object_path=receipt.object_path,
            expected_metadata=metadata,
        )
        == receipt
    )

    stale = store.create_multipart_upload(
        object_path=receipt.object_path,
        content_type="application/vnd.riverhog.pack-volume+age",
        metadata=metadata,
    )
    stale_part = store.upload_part(upload=stale, number=1, content=b"different!!")
    recovered = store.complete_multipart_upload(
        upload=stale,
        parts=(stale_part,),
        expected_bytes=11,
        expected_metadata=metadata,
    )
    assert recovered == receipt


def test_multipart_adapter_rejects_an_existing_different_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeClient()
    monkeypatch.setattr(
        "riverhog_core.stores.s3_archive_ingress_store.create_archive_s3_client",
        lambda *_args, **_kwargs: client,
    )
    config = _config(tmp_path)
    store = S3ArchiveMultipartObjectStore(config, config.archive_store("primary"))
    client.objects["archive/volume.age"] = {
        "Body": b"other",
        "ContentLength": 5,
        "Metadata": {"riverhog-plan-sha256": "b" * 64},
        "LastModified": datetime(2026, 1, 1, tzinfo=UTC),
    }

    with pytest.raises(ArchiveObjectIdentityConflict):
        store.head_completed_object(
            object_path="archive/volume.age",
            expected_metadata={"riverhog-plan-sha256": "a" * 64},
        )


def test_immutable_adapter_is_idempotent_by_logical_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeClient()
    monkeypatch.setattr(
        "riverhog_core.stores.s3_archive_manifest_store.create_archive_s3_client",
        lambda *_args, **_kwargs: client,
    )
    config = _config(tmp_path)
    store = S3ImmutableArchiveObjectStore(config, config.archive_store("primary"))

    first = store.put_immutable_object(
        object_path="archive/manifest.json.age",
        content=b"ciphertext",
        content_type="application/vnd.riverhog.collection-manifest+age",
        identity_metadata={"riverhog-plaintext-sha256": "a" * 64},
    )
    second = store.put_immutable_object(
        object_path="archive/manifest.json.age",
        content=b"different randomized ciphertext",
        content_type="application/vnd.riverhog.collection-manifest+age",
        identity_metadata={"riverhog-plaintext-sha256": "a" * 64},
    )

    assert second == first
    assert client.objects[first.object_path]["Body"] == b"ciphertext"
    assert first.stored_sha256 == hashlib.sha256(b"ciphertext").hexdigest()


def test_immutable_adapter_uses_conditional_multipart_when_put_condition_is_unsupported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeClient(conditional_put_supported=False)
    monkeypatch.setattr(
        "riverhog_core.stores.s3_archive_manifest_store.create_archive_s3_client",
        lambda *_args, **_kwargs: client,
    )
    config = _config(tmp_path)
    store = S3ImmutableArchiveObjectStore(config, config.archive_store("primary"))

    first = store.put_immutable_object(
        object_path="archive/manifest.json.age",
        content=b"ciphertext",
        content_type="application/vnd.riverhog.collection-manifest+age",
        identity_metadata={"riverhog-plaintext-sha256": "a" * 64},
    )
    second = store.put_immutable_object(
        object_path="archive/manifest.json.age",
        content=b"different randomized ciphertext",
        content_type="application/vnd.riverhog.collection-manifest+age",
        identity_metadata={"riverhog-plaintext-sha256": "a" * 64},
    )

    assert client.put_attempts == 1
    assert second == first
    assert client.objects[first.object_path]["Body"] == b"ciphertext"
    assert first.stored_sha256 == hashlib.sha256(b"ciphertext").hexdigest()


def test_range_adapter_returns_the_exact_requested_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeClient()
    client.objects["archive/volume.age"] = {"Body": b"0123456789"}
    monkeypatch.setattr(
        "riverhog_core.stores.s3_archive_range_store.create_archive_s3_client",
        lambda *_args, **_kwargs: client,
    )
    config = _config(tmp_path)
    store = S3ArchiveObjectRangeStore(
        config,
        config.archive_store("primary"),
        read_chunk_bytes=64 * 1024,
    )

    assert (
        b"".join(
            store.iter_object_range(
                object_path="archive/volume.age",
                version_id=None,
                offset=3,
                size=4,
            )
        )
        == b"3456"
    )

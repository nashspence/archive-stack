from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from riverhog_age import decrypt_age_scrypt
from riverhog_core.collection_archives import (
    CollectionArchiveFile,
    CollectionArchivePackage,
    build_collection_archive_package,
)
from riverhog_core.ports.archive_store import (
    ArchiveMultipartUploadedPart,
    ArchiveMultipartUploadState,
    ArchiveMultipartUploadTracker,
    ArchiveObjectIdentity,
    ArchivePackageVerificationError,
    CollectionArchivePackageIdentity,
    CollectionArchiveUploadReceipt,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.stores.s3_archive_store import (
    AGE_SCRYPT_ENCRYPTION,
    COLLECTION_BYTES_METADATA,
    COLLECTION_SHA256_METADATA,
    ENCRYPTION_METADATA,
    PLAINTEXT_BYTES_METADATA,
    PLAINTEXT_SHA256_METADATA,
    S3ArchiveStore,
)
from tests.fixtures.crypto import FixtureProofStamper
from tests.fixtures.data import DOCS_FILES
from tests.unit.db_helpers import sqlite_url

DOCS_ARCHIVE_PREFIX = "archive/archives/opaque-docs"
LARGE_DOCS_ARCHIVE_PREFIX = "archive/archives/opaque-2025/20250104T030405Z__large-docs"


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
        self.head_object_keys: list[str] = []
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
        self.head_object_keys.append(Key)
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

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        _ = Bucket
        self.objects.pop(Key, None)


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
        database_url=sqlite_url(tmp_path / "state.sqlite3"),
    )
    store_field_by_old_name = {
        "archive_backend": "backend",
        "archive_endpoint_url": "endpoint_url",
        "archive_region": "region",
        "archive_bucket": "bucket",
        "archive_access_key_id": "access_key_id",
        "archive_secret_access_key": "secret_access_key",
        "archive_force_path_style": "force_path_style",
        "archive_prefix": "prefix",
        "archive_storage_class": "storage_class",
        "archive_read_mode": "read_mode",
    }
    store_overrides = {
        store_field_by_old_name.pop(name, name): overrides.pop(name)
        for name in tuple(overrides)
        if name in store_field_by_old_name
    }
    config = replace(config, **overrides)
    store = replace(config.archive_store("deep"), **store_overrides)
    return replace(config, archive_stores={"deep": store})


def _package() -> CollectionArchivePackage:
    return build_collection_archive_package(
        collection_id="2025/20250102T030405Z__docs",
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


def _large_package(size: int = 6 * 1024 * 1024) -> CollectionArchivePackage:
    content = bytes((i * 17 + 3) % 256 for i in range(size))
    return build_collection_archive_package(
        collection_id="2025/20250104T030405Z__large-docs",
        files=(
            CollectionArchiveFile(
                path="video.bin",
                content=content,
                sha256=hashlib.sha256(content).hexdigest(),
            ),
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
        "riverhog_core.stores.s3_archive_store.create_archive_s3_client",
        lambda config, store: client,
    )
    config = _config(tmp_path, **config_overrides)
    return S3ArchiveStore(config, config.archive_store("deep"))


def _package_identity(
    receipt: CollectionArchiveUploadReceipt,
) -> CollectionArchivePackageIdentity:
    return CollectionArchivePackageIdentity(
        archive=ArchiveObjectIdentity(
            object_path=receipt.archive.object_path,
            stored_bytes=receipt.archive.stored_bytes,
            sha256=receipt.archive_sha256,
        ),
        manifest=ArchiveObjectIdentity(
            object_path=receipt.manifest.object_path,
            stored_bytes=receipt.manifest.stored_bytes,
            sha256=receipt.manifest_sha256,
        ),
        proof=ArchiveObjectIdentity(
            object_path=receipt.proof.object_path,
            stored_bytes=receipt.proof.stored_bytes,
            sha256=receipt.proof_sha256,
        ),
    )


def test_upload_collection_archive_package_uploads_encrypted_manifest_and_proof_objects(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    passphrase = "aws encrypted archive test passphrase"
    store = _store_with_client(
        monkeypatch,
        tmp_path,
        client,
        archive_backend="aws",
        archive_endpoint_url="https://s3.us-west-2.amazonaws.com",
        archive_storage_class="DEEP_ARCHIVE",
        archive_passphrase=passphrase,
        archive_work_factor=12,
    )
    package = _package()

    receipt = store.upload_collection_archive_package(
        collection_id="2025/20250102T030405Z__docs",
        package=package,
        archive_storage_prefix=DOCS_ARCHIVE_PREFIX,
    )

    assert receipt.archive.object_path == f"{DOCS_ARCHIVE_PREFIX}/archive.tar.age"
    assert receipt.manifest.object_path == f"{DOCS_ARCHIVE_PREFIX}/manifest.yml.age"
    assert receipt.proof.object_path == f"{DOCS_ARCHIVE_PREFIX}/manifest.yml.ots.age"
    assert "collections/docs" not in receipt.archive.object_path
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
    assert manifest_head["Body"] != package.manifest_bytes
    assert proof_head["Body"] != package.proof_bytes
    assert decrypt_age_scrypt(manifest_head["Body"], passphrase) == package.manifest_bytes
    assert decrypt_age_scrypt(proof_head["Body"], passphrase) == package.proof_bytes
    assert archive_head["StorageClass"] == "DEEP_ARCHIVE"
    assert "StorageClass" not in manifest_head
    assert "StorageClass" not in proof_head
    archive_metadata = archive_head["Metadata"]
    assert archive_metadata[ENCRYPTION_METADATA] == AGE_SCRYPT_ENCRYPTION
    assert archive_metadata[COLLECTION_BYTES_METADATA] == str(len(package.archive_bytes))
    assert archive_metadata[COLLECTION_SHA256_METADATA] == package.archive_sha256
    assert archive_metadata["riverhog-archive-format"] == "tar"
    assert archive_metadata["riverhog-compression"] == "none"
    assert archive_metadata["riverhog-storage-class"] == "DEEP_ARCHIVE"
    assert manifest_head["Metadata"]["riverhog-storage-class"] == "STANDARD"
    assert proof_head["Metadata"]["riverhog-storage-class"] == "STANDARD"


def test_verify_collection_archive_package_checks_each_remote_object(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    store = _store_with_client(
        monkeypatch,
        tmp_path,
        client,
        archive_backend="aws",
        archive_endpoint_url="https://s3.us-west-2.amazonaws.com",
        archive_storage_class="DEEP_ARCHIVE",
        archive_work_factor=12,
    )
    receipt = store.upload_collection_archive_package(
        collection_id="2025/20250102T030405Z__docs",
        package=_package(),
        archive_storage_prefix=DOCS_ARCHIVE_PREFIX,
    )
    client.head_object_keys.clear()

    store.verify_collection_archive_package(
        collection_id="2025/20250102T030405Z__docs",
        package=_package_identity(receipt),
    )

    assert client.head_object_keys == [
        receipt.archive.object_path,
        receipt.manifest.object_path,
        receipt.proof.object_path,
    ]


def test_verify_collection_archive_package_rejects_changed_checksum(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    store = _store_with_client(
        monkeypatch,
        tmp_path,
        client,
        archive_work_factor=12,
    )
    receipt = store.upload_collection_archive_package(
        collection_id="2025/20250102T030405Z__docs",
        package=_package(),
        archive_storage_prefix=DOCS_ARCHIVE_PREFIX,
    )
    client.objects[receipt.manifest.object_path]["Metadata"][COLLECTION_SHA256_METADATA] = "0" * 64

    with pytest.raises(ArchivePackageVerificationError, match="manifest object does not match"):
        store.verify_collection_archive_package(
            collection_id="2025/20250102T030405Z__docs",
            package=_package_identity(receipt),
        )


def test_verify_collection_archive_package_requires_recovery_proof(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    store = _store_with_client(
        monkeypatch,
        tmp_path,
        client,
        archive_work_factor=12,
    )
    receipt = store.upload_collection_archive_package(
        collection_id="2025/20250102T030405Z__docs",
        package=_package(),
        archive_storage_prefix=DOCS_ARCHIVE_PREFIX,
    )
    client.objects.pop(receipt.proof.object_path)

    with pytest.raises(ArchivePackageVerificationError, match="manifest-proof object is missing"):
        store.verify_collection_archive_package(
            collection_id="2025/20250102T030405Z__docs",
            package=_package_identity(receipt),
        )


def test_encrypted_collection_archive_package_uploads_age_objects_and_restores_plaintext(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    passphrase = "garage encrypted archive test passphrase"
    store = _store_with_client(
        monkeypatch,
        tmp_path,
        client,
        archive_encryption="age_scrypt",
        archive_passphrase=passphrase,
        archive_work_factor=12,
    )
    package = _package()

    receipt = store.upload_collection_archive_package(
        collection_id="2025/20250102T030405Z__docs",
        package=package,
        archive_storage_prefix=DOCS_ARCHIVE_PREFIX,
    )

    assert receipt.archive.object_path == f"{DOCS_ARCHIVE_PREFIX}/archive.tar.age"
    assert receipt.manifest.object_path == f"{DOCS_ARCHIVE_PREFIX}/manifest.yml.age"
    assert receipt.proof.object_path == f"{DOCS_ARCHIVE_PREFIX}/manifest.yml.ots.age"
    assert "collections/docs" not in receipt.archive.object_path
    archive_head = client.objects[receipt.archive.object_path]
    manifest_head = client.objects[receipt.manifest.object_path]
    proof_head = client.objects[receipt.proof.object_path]
    assert archive_head["Body"] != package.archive_bytes
    assert manifest_head["Body"] != package.manifest_bytes
    assert proof_head["Body"] != package.proof_bytes
    assert decrypt_age_scrypt(manifest_head["Body"], passphrase) == package.manifest_bytes
    assert decrypt_age_scrypt(proof_head["Body"], passphrase) == package.proof_bytes
    assert archive_head["Metadata"][ENCRYPTION_METADATA] == AGE_SCRYPT_ENCRYPTION
    assert archive_head["Metadata"][COLLECTION_BYTES_METADATA] == str(package.archive_size)
    assert archive_head["Metadata"][COLLECTION_SHA256_METADATA] == package.archive_sha256
    assert archive_head["Metadata"][PLAINTEXT_BYTES_METADATA] == str(package.archive_size)
    assert archive_head["Metadata"][PLAINTEXT_SHA256_METADATA] == package.archive_sha256
    assert receipt.archive.stored_bytes == len(archive_head["Body"])
    assert receipt.archive_sha256 == package.archive_sha256
    assert receipt.manifest_sha256 == package.manifest_sha256

    restored_archive = b"".join(
        store.iter_collection_archive(
            collection_id="2025/20250102T030405Z__docs",
            object_path=receipt.archive.object_path,
        )
    )
    assert restored_archive == package.archive_bytes
    assert (
        store.read_collection_manifest(
            collection_id="2025/20250102T030405Z__docs",
            object_path=receipt.manifest.object_path,
        )
        == package.manifest_bytes
    )
    assert (
        store.read_collection_manifest_proof(
            collection_id="2025/20250102T030405Z__docs",
            object_path=receipt.proof.object_path,
        )
        == package.proof_bytes
    )


def test_delete_collection_archive_package_removes_and_verifies_owned_objects(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    store = _store_with_client(monkeypatch, tmp_path, client)
    receipt = store.upload_collection_archive_package(
        collection_id="2025/20250102T030405Z__docs",
        package=_package(),
        archive_storage_prefix=DOCS_ARCHIVE_PREFIX,
    )

    store.delete_collection_archive_package(
        collection_id="2025/20250102T030405Z__docs",
        object_path=receipt.archive.object_path,
        manifest_object_path=receipt.manifest.object_path,
        proof_object_path=receipt.proof.object_path,
    )

    assert client.objects == {}


def test_publish_restore_catalog_writes_generic_readme_and_encrypted_catalog(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    passphrase = "catalog archive passphrase"
    store = _store_with_client(
        monkeypatch,
        tmp_path,
        client,
        archive_passphrase=passphrase,
        archive_work_factor=12,
    )

    store.publish_restore_catalog(
        generated_at="2026-06-03T06:00:00Z",
        entries=[
            {
                "collection_id": "2026/20260603T060000Z__private-family-photos",
                "archive_storage_prefix": DOCS_ARCHIVE_PREFIX,
                "archive_key": f"{DOCS_ARCHIVE_PREFIX}/archive.tar.age",
                "manifest_key": f"{DOCS_ARCHIVE_PREFIX}/manifest.yml.age",
                "proof_key": f"{DOCS_ARCHIVE_PREFIX}/manifest.yml.ots.age",
                "archive_stored_bytes": 123,
                "archive_plaintext_sha256": "a" * 64,
                "backend": "s3",
                "archive_storage_class": "DEEP_ARCHIVE",
            }
        ],
    )

    readme = client.objects["archive/README.md"]["Body"].decode("utf-8")
    agents = client.objects["archive/AGENTS.md"]["Body"].decode("utf-8")
    assert "sole durable copies" in readme
    assert "permanently destroy the only recoverable copy" in readme
    assert "guarded archive workflows" in readme
    assert "S3 credentials, token, or S3 login/session" in readme
    assert "will fail until the CLI is authenticated" in readme
    assert "AWS_SESSION_TOKEN" in readme
    assert "archive passphrase" in readme
    assert "age --decrypt -o collections.yml collections.yml.age" in readme
    assert "Enter the archive passphrase when age prompts." in readme
    assert "sole durable copies" in agents
    assert "Treat this archive root as read-only" in agents
    assert "Never infer cleanup from object age" in agents
    assert "catalog/collections.yml.age" in agents
    assert "private-family-photos" not in agents
    catalog_object = client.objects["archive/catalog/collections.yml.age"]
    assert b"private-family-photos" not in catalog_object["Body"]
    assert catalog_object["Metadata"][ENCRYPTION_METADATA] == AGE_SCRYPT_ENCRYPTION
    catalog = yaml.safe_load(decrypt_age_scrypt(catalog_object["Body"], passphrase))
    assert catalog == {
        "format": "encrypted-archive-catalog-v1",
        "generated_at": "2026-06-03T06:00:00Z",
        "archives": [
            {
                "collection_id": "2026/20260603T060000Z__private-family-photos",
                "archive_storage_prefix": DOCS_ARCHIVE_PREFIX,
                "archive_key": f"{DOCS_ARCHIVE_PREFIX}/archive.tar.age",
                "manifest_key": f"{DOCS_ARCHIVE_PREFIX}/manifest.yml.age",
                "proof_key": f"{DOCS_ARCHIVE_PREFIX}/manifest.yml.ots.age",
                "archive_stored_bytes": 123,
                "archive_plaintext_sha256": "a" * 64,
                "backend": "s3",
                "archive_storage_class": "DEEP_ARCHIVE",
                "archive_id": "opaque-docs",
            }
        ],
    }


def test_dedicated_archive_bucket_uses_bucket_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    passphrase = "dedicated bucket passphrase"
    store = _store_with_client(
        monkeypatch,
        tmp_path,
        client,
        archive_prefix="",
        archive_passphrase=passphrase,
        archive_work_factor=12,
    )

    storage_prefix = store.new_collection_archive_storage_prefix()
    assert storage_prefix.startswith("archives/")

    receipt = store.upload_collection_archive_package(
        collection_id="2026/20260603T060000Z__bucket-root",
        package=_package(),
        archive_storage_prefix="archives/opaque-root",
    )
    assert {
        receipt.archive.object_path,
        receipt.manifest.object_path,
        receipt.proof.object_path,
    } == {
        "archives/opaque-root/archive.tar.age",
        "archives/opaque-root/manifest.yml.age",
        "archives/opaque-root/manifest.yml.ots.age",
    }
    store.delete_collection_archive_package(
        collection_id="2026/20260603T060000Z__bucket-root",
        object_path=receipt.archive.object_path,
        manifest_object_path=receipt.manifest.object_path,
        proof_object_path=receipt.proof.object_path,
    )
    assert client.objects == {}

    store.publish_restore_catalog(
        generated_at="2026-06-03T06:00:00Z",
        entries=[{"archive_storage_prefix": "archives/opaque-root"}],
    )

    assert set(client.objects) == {
        "AGENTS.md",
        "README.md",
        "catalog/collections.yml.age",
    }
    assert "omit it when this guidance file is stored at the bucket root" in client.objects[
        "README.md"
    ]["Body"].decode("utf-8")
    catalog = yaml.safe_load(
        decrypt_age_scrypt(client.objects["catalog/collections.yml.age"]["Body"], passphrase)
    )
    assert catalog["archives"] == [
        {
            "archive_storage_prefix": "archives/opaque-root",
            "archive_id": "opaque-root",
        }
    ]


def test_encrypted_collection_archive_package_resumes_existing_multipart_upload(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    passphrase = "encrypted multipart resume passphrase"
    store = _store_with_client(
        monkeypatch,
        tmp_path,
        client,
        archive_encryption="age_scrypt",
        archive_passphrase=passphrase,
        archive_work_factor=12,
        archive_multipart_part_bytes=5 * 1024 * 1024,
        archive_multipart_concurrency=1,
    )
    tracker = _FakeMultipartTracker()
    package = _large_package()
    client.fail_next_upload_part_after_successes = 1

    with pytest.raises(RuntimeError, match="synthetic upload_part failure"):
        store.upload_collection_archive_package(
            collection_id="2025/20250104T030405Z__large-docs",
            package=package,
            archive_storage_prefix=LARGE_DOCS_ARCHIVE_PREFIX,
            multipart_tracker=tracker,
        )

    assert tracker.state is not None
    assert tracker.state.encryption_state_json is not None
    assert tracker.state.total_parts == 2
    assert tracker.parts[0].part_number == 1
    assert client.aborted_uploads == []
    first_upload_id = tracker.state.upload_id
    first_part = client.uploads[first_upload_id]["Parts"][1]

    receipt = store.upload_collection_archive_package(
        collection_id="2025/20250104T030405Z__large-docs",
        package=package,
        archive_storage_prefix=LARGE_DOCS_ARCHIVE_PREFIX,
        multipart_tracker=tracker,
    )

    assert receipt.archive.object_path == f"{LARGE_DOCS_ARCHIVE_PREFIX}/archive.tar.age"
    assert client.completed_uploads == [first_upload_id]
    assert client.aborted_uploads == []
    assert tracker.state is None
    assert tracker.cleared == [first_upload_id]
    assert client.uploads == {}
    assert client.objects[receipt.archive.object_path]["Body"].startswith(first_part)
    restored_archive = b"".join(
        store.iter_collection_archive(
            collection_id="2025/20250104T030405Z__large-docs",
            object_path=receipt.archive.object_path,
        )
    )
    assert restored_archive == package.archive_bytes


def test_encrypted_archive_upload_accepts_a_sequential_source_stream(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    passphrase = "sequential archive source passphrase"
    store = _store_with_client(
        monkeypatch,
        tmp_path,
        client,
        archive_encryption="age_scrypt",
        archive_passphrase=passphrase,
        archive_work_factor=12,
        archive_multipart_part_bytes=5 * 1024 * 1024,
        archive_multipart_concurrency=1,
    )
    package = replace(_large_package(), _archive_chunks_from_offset=None)

    receipt = store.upload_collection_archive_package(
        collection_id="2025/20250104T030405Z__large-docs",
        package=package,
        archive_storage_prefix=LARGE_DOCS_ARCHIVE_PREFIX,
    )

    restored_archive = b"".join(
        store.iter_collection_archive(
            collection_id="2025/20250104T030405Z__large-docs",
            object_path=receipt.archive.object_path,
        )
    )
    assert restored_archive == package.archive_bytes


def test_interrupted_sequential_archive_upload_aborts_incomplete_multipart(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    store = _store_with_client(
        monkeypatch,
        tmp_path,
        client,
        archive_passphrase="sequential archive interruption passphrase",
        archive_work_factor=12,
        archive_multipart_part_bytes=5 * 1024 * 1024,
        archive_multipart_concurrency=1,
    )
    package = replace(_large_package(), _archive_chunks_from_offset=None)
    client.fail_next_upload_part_after_successes = 1

    with pytest.raises(RuntimeError, match="synthetic upload_part failure"):
        store.upload_collection_archive_package(
            collection_id="2025/20250104T030405Z__large-docs",
            package=package,
            archive_storage_prefix=LARGE_DOCS_ARCHIVE_PREFIX,
        )

    assert client.aborted_uploads == ["upload-1"]
    assert client.uploads == {}


def test_prepare_collection_archive_read_requests_collection_manifest_and_proof(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    store = _store_with_client(
        monkeypatch,
        tmp_path,
        client,
        archive_backend="aws",
        archive_endpoint_url="https://s3.us-west-2.amazonaws.com",
        archive_storage_class="DEEP_ARCHIVE",
    )
    package = _package()
    receipt = store.upload_collection_archive_package(
        collection_id="2025/20250102T030405Z__docs",
        package=package,
        archive_storage_prefix=DOCS_ARCHIVE_PREFIX,
    )

    status = store.prepare_collection_archive_read(
        collection_id="2025/20250102T030405Z__docs",
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
        archive_backend="aws",
        archive_endpoint_url="https://s3.us-west-2.amazonaws.com",
        archive_storage_class="INTELLIGENT_TIERING",
    )
    package = _package()
    receipt = store.upload_collection_archive_package(
        collection_id="2025/20250102T030405Z__docs",
        package=package,
        archive_storage_prefix=DOCS_ARCHIVE_PREFIX,
    )
    client.objects[receipt.archive.object_path]["ArchiveCopyStatus"] = "ARCHIVE_ACCESS"

    status = store.get_collection_archive_read_status(
        collection_id="2025/20250102T030405Z__docs",
        object_path=receipt.archive.object_path,
        requested_at="2026-04-20T04:00:00Z",
        estimated_ready_at="2026-04-20T16:00:00Z",
        estimated_expires_at=None,
    )

    assert status.state == "requested"


def test_iter_collection_archive_streams_when_ready(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    store = _store_with_client(monkeypatch, tmp_path, client)
    package = _package()
    receipt = store.upload_collection_archive_package(
        collection_id="2025/20250102T030405Z__docs",
        package=package,
        archive_storage_prefix=DOCS_ARCHIVE_PREFIX,
    )

    chunks = list(
        store.iter_collection_archive(
            collection_id="2025/20250102T030405Z__docs",
            object_path=receipt.archive.object_path,
        )
    )

    assert b"".join(chunks) == package.archive_bytes

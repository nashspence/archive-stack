from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from riverhog_age import decrypt_age_scrypt
from riverhog_core.collection_archives import (
    CollectionArchiveFile,
    build_collection_archive_package,
)
from riverhog_core.ports.archive_store import (
    ArchiveMultipartUploadedPart,
    ArchiveMultipartUploadState,
    ArchiveMultipartUploadTracker,
)
from riverhog_core.runtime_config import load_runtime_config
from riverhog_core.stores.s3_archive_store import (
    AGE_SCRYPT_ENCRYPTION,
    ENCRYPTION_METADATA,
    S3ArchiveStore,
)
from riverhog_core.stores.s3_support import create_archive_s3_client
from tests.fixtures.crypto import FixtureProofStamper

pytestmark = pytest.mark.integration


class _FailingUploadPartClient:
    def __init__(self, inner: Any, *, fail_after_successes: int) -> None:
        self._inner = inner
        self._fail_after_successes = fail_after_successes
        self._successful_upload_parts = 0

    def upload_part(self, **kwargs: Any) -> dict[str, Any]:
        if self._successful_upload_parts >= self._fail_after_successes:
            self._fail_after_successes = 10**9
            raise RuntimeError("synthetic Garage multipart interruption")
        response = self._inner.upload_part(**kwargs)
        self._successful_upload_parts += 1
        return response

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _MemoryMultipartTracker(ArchiveMultipartUploadTracker):
    def __init__(self) -> None:
        self.state: ArchiveMultipartUploadState | None = None
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
        _ = collection_id, state, uploaded_bytes, uploaded_parts, total_parts
        self.parts = [current for current in self.parts if current.part_number != part.part_number]
        self.parts.append(part)
        self.parts.sort(key=lambda current: current.part_number)

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


def _large_package():
    content = bytes((i * 37 + 9) % 256 for i in range(6 * 1024 * 1024))
    return build_collection_archive_package(
        collection_id="2025/20250105T030405Z__garage-encrypted",
        files=(
            CollectionArchiveFile(
                path="camera/video.bin",
                content=content,
                sha256=hashlib.sha256(content).hexdigest(),
            ),
        ),
        stamper=FixtureProofStamper(),
    )


def _delete_prefix(client: Any, *, bucket: str, prefix: str) -> None:
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        objects = [{"Key": entry["Key"]} for entry in page.get("Contents", [])]
        if objects:
            client.delete_objects(Bucket=bucket, Delete={"Objects": objects})


def _abort_multipart_uploads(client: Any, *, bucket: str, prefix: str) -> None:
    response = client.list_multipart_uploads(Bucket=bucket, Prefix=prefix)
    for upload in response.get("Uploads", []):
        client.abort_multipart_upload(
            Bucket=bucket,
            Key=upload["Key"],
            UploadId=upload["UploadId"],
        )


def _ensure_bucket_exists(client: Any, *, bucket: str) -> None:
    client.head_bucket(Bucket=bucket)


def _body_chunks(body: Any) -> Iterator[bytes]:
    try:
        yield from body.iter_chunks(chunk_size=1024 * 1024)
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            close()


def test_encrypted_archive_multipart_resume_and_restore_against_garage(tmp_path: Path):
    _ = tmp_path
    if os.environ.get("RIVERHOG_GARAGE_ARCHIVE_ENCRYPTION_TEST") != "1":
        pytest.skip("set RIVERHOG_GARAGE_ARCHIVE_ENCRYPTION_TEST=1 to run against Garage")

    prefix = f"2025/20250105T030405Z__garage-encrypted-test/{uuid.uuid4().hex}"
    archive_storage_prefix = f"{prefix}/archives/opaque"
    passphrase = os.environ.get(
        "RIVERHOG_ARCHIVE_PASSPHRASE",
        "garage encrypted archive integration passphrase",
    )
    base_config = load_runtime_config()
    archive_store_config = replace(
        base_config.archive_store(base_config.default_archive_store),
        prefix=prefix,
    )
    config = replace(
        base_config,
        archive_stores={archive_store_config.name: archive_store_config},
        archive_encryption="age_scrypt",
        archive_passphrase=passphrase,
        archive_work_factor=12,
        archive_multipart_part_bytes=5 * 1024 * 1024,
        archive_multipart_concurrency=1,
    )
    package = _large_package()
    tracker = _MemoryMultipartTracker()
    real_client = create_archive_s3_client(config, archive_store_config)
    _ensure_bucket_exists(real_client, bucket=archive_store_config.bucket)
    failing_client = _FailingUploadPartClient(real_client, fail_after_successes=1)
    store = S3ArchiveStore(config, archive_store_config)
    store._client = failing_client  # type: ignore[attr-defined]

    try:
        with pytest.raises(RuntimeError, match="synthetic Garage multipart interruption"):
            store.upload_collection_archive_package(
                collection_id="2025/20250105T030405Z__garage-encrypted",
                package=package,
                archive_storage_prefix=archive_storage_prefix,
                multipart_tracker=tracker,
            )

        assert tracker.state is not None
        assert tracker.state.encryption_state_json is not None
        assert tracker.parts and tracker.parts[0].part_number == 1
        upload_id = tracker.state.upload_id

        store._client = real_client  # type: ignore[attr-defined]
        receipt = store.upload_collection_archive_package(
            collection_id="2025/20250105T030405Z__garage-encrypted",
            package=package,
            archive_storage_prefix=archive_storage_prefix,
            multipart_tracker=tracker,
        )

        assert tracker.cleared == [upload_id]
        assert receipt.archive.object_path.endswith("/archive.tar.age")
        assert receipt.manifest.object_path.endswith("/manifest.yml.age")
        assert receipt.proof.object_path.endswith("/manifest.yml.ots.age")

        archive_head = real_client.head_object(
            Bucket=archive_store_config.bucket,
            Key=receipt.archive.object_path,
        )
        assert archive_head["Metadata"][ENCRYPTION_METADATA] == AGE_SCRYPT_ENCRYPTION
        manifest_object = real_client.get_object(
            Bucket=archive_store_config.bucket,
            Key=receipt.manifest.object_path,
        )
        manifest_ciphertext = b"".join(_body_chunks(manifest_object["Body"]))
        assert decrypt_age_scrypt(manifest_ciphertext, passphrase) == package.manifest_bytes
        restored_archive = b"".join(
            store.iter_collection_archive(
                collection_id="2025/20250105T030405Z__garage-encrypted",
                object_path=receipt.archive.object_path,
            )
        )
        assert restored_archive == package.archive_bytes
        assert (
            store.read_collection_manifest(
                collection_id="2025/20250105T030405Z__garage-encrypted",
                object_path=receipt.manifest.object_path,
            )
            == package.manifest_bytes
        )
        assert (
            store.read_collection_manifest_proof(
                collection_id="2025/20250105T030405Z__garage-encrypted",
                object_path=receipt.proof.object_path,
            )
            == package.proof_bytes
        )
    finally:
        _abort_multipart_uploads(real_client, bucket=archive_store_config.bucket, prefix=prefix)
        _delete_prefix(real_client, bucket=archive_store_config.bucket, prefix=prefix)

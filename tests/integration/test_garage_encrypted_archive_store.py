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
from riverhog_core.archive_objects import (
    CollectionArchiveSourceFile,
    build_collection_archive,
)
from riverhog_core.ports.archive_store import (
    ArchiveMultipartUploadedPart,
    ArchiveMultipartUploadState,
    ArchiveMultipartUploadTracker,
    ArchiveObjectIdentity,
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
        collection_id: int,
        object_id: str,
        object_path: str,
        part_size: int,
        content_length: int,
        sha256: str,
    ) -> ArchiveMultipartUploadState | None:
        _ = collection_id, object_id
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
        collection_id: int,
        state: ArchiveMultipartUploadState,
    ) -> None:
        _ = collection_id
        self.state = state
        self.parts = list(state.parts)

    def record_multipart_upload_progress(
        self,
        *,
        collection_id: int,
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
        collection_id: int,
        object_id: str,
        upload_id: str,
    ) -> None:
        _ = collection_id, object_id
        self.cleared.append(upload_id)
        self.state = None
        self.parts = []


def _large_archive():
    content = bytes((i * 37 + 9) % 256 for i in range(6 * 1024 * 1024))
    return build_collection_archive(
        collection_id=1,
        files=(
            CollectionArchiveSourceFile(
                path="camera/video.bin",
                content=content,
                sha256=hashlib.sha256(content).hexdigest(),
            ),
        ),
        max_plaintext_object_bytes=32 * 1024 * 1024,
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

    prefix = f"garage-encrypted-test/{uuid.uuid4().hex}"
    archive_storage_prefix = f"{prefix}/archives/opaque"
    passphrase = os.environ.get(
        "RIVERHOG_ARCHIVE_PASSPHRASE",
        "garage encrypted archive integration passphrase",
    )
    base_config = load_runtime_config()
    archive_store_config = replace(
        base_config.archive_store(base_config.archive_write_store),
        prefix=prefix,
    )
    config = replace(
        base_config,
        archive_stores={archive_store_config.name: archive_store_config},
        archive_passphrase=passphrase,
        archive_scrypt_work_factor=12,
        archive_multipart_part_bytes=5 * 1024 * 1024,
        archive_multipart_concurrency=1,
    )
    archive = _large_archive()
    tracker = _MemoryMultipartTracker()
    real_client = create_archive_s3_client(config, archive_store_config)
    _ensure_bucket_exists(real_client, bucket=archive_store_config.bucket)
    failing_client = _FailingUploadPartClient(real_client, fail_after_successes=1)
    store = S3ArchiveStore(config, archive_store_config)
    store._client = failing_client  # type: ignore[attr-defined]

    try:
        with pytest.raises(RuntimeError, match="synthetic Garage multipart interruption"):
            store.upload_collection_archive(
                collection_id=1,
                archive=archive,
                archive_storage_prefix=archive_storage_prefix,
                multipart_tracker=tracker,
            )

        assert tracker.state is not None
        assert tracker.state.encryption_state_json is not None
        assert tracker.parts and tracker.parts[0].part_number == 1
        upload_id = tracker.state.upload_id

        store._client = real_client  # type: ignore[attr-defined]
        receipt = store.upload_collection_archive(
            collection_id=1,
            archive=archive,
            archive_storage_prefix=archive_storage_prefix,
            multipart_tracker=tracker,
        )

        assert tracker.cleared == [upload_id]
        data = receipt.require_object("data-000000")
        manifest = receipt.require_object("manifest")
        proof = receipt.require_object("proof")
        assert data.object_path.endswith("/objects/data-000000.age")
        assert manifest.object_path.endswith("/manifest.yml.age")
        assert proof.object_path.endswith("/manifest.yml.ots.age")

        archive_head = real_client.head_object(
            Bucket=archive_store_config.bucket,
            Key=data.object_path,
        )
        assert archive_head["Metadata"][ENCRYPTION_METADATA] == AGE_SCRYPT_ENCRYPTION
        manifest_object = real_client.get_object(
            Bucket=archive_store_config.bucket,
            Key=manifest.object_path,
        )
        manifest_ciphertext = b"".join(_body_chunks(manifest_object["Body"]))
        assert decrypt_age_scrypt(manifest_ciphertext, passphrase) == archive.manifest_bytes
        restored_data = b"".join(
            store.iter_archive_object(
                collection_id=1,
                object=ArchiveObjectIdentity(
                    object_id=data.object_id,
                    kind=data.kind,
                    object_path=data.object_path,
                    plaintext_bytes=data.plaintext_bytes,
                    stored_bytes=data.stored_bytes,
                    sha256=data.sha256,
                    stored_sha256=data.stored_sha256,
                ),
            )
        )
        assert restored_data == b"".join(archive.data_objects[0].iter_plaintext())
        restored_manifest = b"".join(
            store.iter_archive_object(
                collection_id=1,
                object=ArchiveObjectIdentity(
                    object_id=manifest.object_id,
                    kind=manifest.kind,
                    object_path=manifest.object_path,
                    plaintext_bytes=manifest.plaintext_bytes,
                    stored_bytes=manifest.stored_bytes,
                    sha256=manifest.sha256,
                    stored_sha256=manifest.stored_sha256,
                ),
            )
        )
        assert restored_manifest == archive.manifest_bytes
        restored_proof = b"".join(
            store.iter_archive_object(
                collection_id=1,
                object=ArchiveObjectIdentity(
                    object_id=proof.object_id,
                    kind=proof.kind,
                    object_path=proof.object_path,
                    plaintext_bytes=proof.plaintext_bytes,
                    stored_bytes=proof.stored_bytes,
                    sha256=proof.sha256,
                    stored_sha256=proof.stored_sha256,
                ),
            )
        )
        assert restored_proof == archive.proof_bytes
    finally:
        _abort_multipart_uploads(real_client, bucket=archive_store_config.bucket, prefix=prefix)
        _delete_prefix(real_client, bucket=archive_store_config.bucket, prefix=prefix)

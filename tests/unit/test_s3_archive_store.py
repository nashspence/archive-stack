from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from riverhog_age import decrypt_age_scrypt, encrypt_age_scrypt
from riverhog_core.ports.archive_store import (
    ArchiveObjectIdentity,
    ArchiveVerificationError,
    CollectionArchiveIdentity,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.stores.s3_archive_store import (
    AGE_SCRYPT_ENCRYPTION,
    COLLECTION_BYTES_METADATA,
    COLLECTION_SHA256_METADATA,
    ENCRYPTION_METADATA,
    PLAINTEXT_BYTES_METADATA,
    PLAINTEXT_SHA256_METADATA,
    STORED_SHA256_METADATA,
    S3ArchiveStore,
)

from tests.unit.db_helpers import sqlite_url

COLLECTION_ID = 1
ARCHIVE_PREFIX = "archive/archives/opaque-docs"


class _MissingObjectError(Exception):
    def __init__(self) -> None:
        self.response = {"Error": {"Code": "404"}}


class _FakeBody:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def iter_chunks(self, *, chunk_size: int) -> Iterator[bytes]:
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset : offset + chunk_size]

    def close(self) -> None:
        return


class _FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.uploads: dict[str, dict[str, Any]] = {}
        self.versions: dict[str, set[str]] = {}
        self.restore_requests: list[tuple[str, object]] = []
        self.aborted_uploads: list[tuple[str, str]] = []
        self.multipart_page_size: int | None = None

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        del Bucket
        if Key not in self.objects:
            raise _MissingObjectError
        return {key: value for key, value in self.objects[Key].items() if key != "Body"}

    def put_object(self, *, Bucket: str, Key: str, Body: object, **kwargs: Any) -> dict[str, str]:
        del Bucket
        body = bytes(Body) if isinstance(Body, (bytes, bytearray)) else b"".join(Body)  # type: ignore[arg-type]
        self.objects[Key] = {
            "Body": body,
            "ContentLength": len(body),
            "LastModified": datetime(2026, 1, 1, tzinfo=UTC),
            **kwargs,
        }
        return {}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        del Bucket
        return {"Body": _FakeBody(cast(bytes, self.objects[Key]["Body"]))}

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        del Bucket
        self.objects.pop(Key, None)

    def get_paginator(self, name: str):  # type: ignore[no-untyped-def]
        assert name in {"list_objects_v2", "list_object_versions"}
        client = self

        class Paginator:
            def paginate(self, *, Bucket: str, Prefix: str):  # type: ignore[no-untyped-def]
                del Bucket
                if name == "list_objects_v2":
                    return [
                        {
                            "Contents": [
                                {"Key": key}
                                for key in sorted(client.objects)
                                if key.startswith(Prefix)
                            ]
                        }
                    ]
                return [
                    {
                        "Versions": [
                            {"Key": key, "VersionId": version_id}
                            for key, version_ids in client.versions.items()
                            if key.startswith(Prefix)
                            for version_id in sorted(version_ids)
                        ]
                    }
                ]

        return Paginator()

    def delete_objects(self, *, Bucket: str, Delete: dict[str, object]) -> None:
        del Bucket
        for entry in cast(list[dict[str, str]], Delete["Objects"]):
            key = entry["Key"]
            version_id = entry.get("VersionId")
            if version_id is None:
                self.objects.pop(key, None)
            else:
                self.versions.get(key, set()).discard(version_id)

    def list_multipart_uploads(
        self,
        *,
        Bucket: str,
        Prefix: str,
        KeyMarker: str = "",
        UploadIdMarker: str = "",
    ) -> dict[str, object]:
        del Bucket
        uploads = sorted(
            (
                {
                    "Key": str(upload["Key"]),
                    "UploadId": upload_id,
                    "Initiated": upload["Initiated"],
                }
                for upload_id, upload in self.uploads.items()
                if str(upload["Key"]).startswith(Prefix)
                and (
                    str(upload["Key"]) > KeyMarker
                    or (str(upload["Key"]) == KeyMarker and upload_id > UploadIdMarker)
                )
            ),
            key=lambda upload: (str(upload["Key"]), str(upload["UploadId"])),
        )
        page_size = self.multipart_page_size or len(uploads)
        page = uploads[:page_size]
        response: dict[str, object] = {
            "IsTruncated": len(page) < len(uploads),
            "Uploads": page,
        }
        if len(page) < len(uploads):
            response["NextKeyMarker"] = page[-1]["Key"]
            response["NextUploadIdMarker"] = page[-1]["UploadId"]
        return response

    def abort_multipart_upload(self, *, Bucket: str, Key: str, UploadId: str) -> None:
        del Bucket
        self.aborted_uploads.append((Key, UploadId))
        self.uploads.pop(UploadId, None)

    def restore_object(self, *, Bucket: str, Key: str, RestoreRequest: object) -> None:
        del Bucket
        self.restore_requests.append((Key, RestoreRequest))
        self.objects[Key]["Restore"] = 'ongoing-request="true"'


def _config(tmp_path: Path, **store_overrides: object) -> RuntimeConfig:
    config = RuntimeConfig(
        archive_passphrase="test-archive-passphrase",
        database_url=sqlite_url(tmp_path / "state.sqlite3"),
    )
    store = replace(
        config.archive_store("archive"),
        **{"name": "deep", "backend": "s3", **store_overrides},
    )
    return replace(
        config,
        archive_stores={"deep": store},
        archive_write_store="deep",
        archive_read_order=("deep",),
    )


def _store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    client: _FakeS3Client,
    **store_overrides: object,
) -> S3ArchiveStore:
    monkeypatch.setattr(
        "riverhog_core.stores.s3_archive_store.create_archive_s3_client",
        lambda config, store: client,
    )
    config = _config(tmp_path, **store_overrides)
    return S3ArchiveStore(config, config.archive_store("deep"))


def _seed_encrypted_object(
    client: _FakeS3Client,
    *,
    path: str,
    kind: str,
    content: bytes,
    storage_class: str = "STANDARD",
) -> ArchiveObjectIdentity:
    ciphertext = encrypt_age_scrypt(content, "test-archive-passphrase", log_n=2)
    plaintext_sha256 = hashlib.sha256(content).hexdigest()
    stored_sha256 = hashlib.sha256(ciphertext).hexdigest()
    client.objects[path] = {
        "Body": ciphertext,
        "ContentLength": len(ciphertext),
        "LastModified": datetime(2026, 1, 1, tzinfo=UTC),
        "StorageClass": storage_class,
        "Metadata": {
            "riverhog-backend": "s3",
            "riverhog-storage-class": storage_class,
            "riverhog-object-kind": f"collection-{kind}",
            "riverhog-object-id": kind,
            ENCRYPTION_METADATA: AGE_SCRYPT_ENCRYPTION,
            PLAINTEXT_BYTES_METADATA: str(len(content)),
            PLAINTEXT_SHA256_METADATA: plaintext_sha256,
            COLLECTION_BYTES_METADATA: str(len(content)),
            COLLECTION_SHA256_METADATA: plaintext_sha256,
            STORED_SHA256_METADATA: stored_sha256,
        },
    }
    return ArchiveObjectIdentity(
        object_id=kind,
        kind=kind,
        object_path=path,
        plaintext_bytes=len(content),
        stored_bytes=len(ciphertext),
        sha256=plaintext_sha256,
        stored_sha256=stored_sha256,
    )


def test_proof_replacement_uses_the_canonical_encrypted_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    store = _store(monkeypatch, tmp_path, client)
    proof_path = f"{ARCHIVE_PREFIX}/manifest.json.ots.age"
    old = _seed_encrypted_object(client, path=proof_path, kind="proof", content=b"old")

    receipt = store.replace_archive_proof(
        collection_id=COLLECTION_ID,
        object=old,
        proof_bytes=b"new-proof",
    )

    assert receipt.object_path == proof_path
    assert (
        decrypt_age_scrypt(
            cast(bytes, client.objects[proof_path]["Body"]),
            "test-archive-passphrase",
        )
        == b"new-proof"
    )


def test_collection_metadata_and_recovery_guidance_are_published(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    store = _store(monkeypatch, tmp_path, client)

    receipt = store.publish_collection_metadata(
        collection_id=COLLECTION_ID,
        archive_storage_prefix=ARCHIVE_PREFIX,
        manifest=b'{"collection":1,"format":"riverhog-collection-metadata/v1"}',
    )

    assert receipt.object_path == f"{ARCHIVE_PREFIX}/metadata.json.age"
    assert (
        decrypt_age_scrypt(
            cast(bytes, client.objects[receipt.object_path]["Body"]),
            "test-archive-passphrase",
        )
        == b'{"collection":1,"format":"riverhog-collection-metadata/v1"}'
    )
    readme = cast(bytes, client.objects["archive/README.md"]["Body"]).decode()
    assert "riverhog-recover" in readme
    assert "manifest.json.age" in readme
    assert "archive/AGENTS.md" in client.objects


def test_archive_object_verification_read_and_deletion_cover_the_identity_set(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    store = _store(monkeypatch, tmp_path, client)
    pack = _seed_encrypted_object(
        client,
        path=f"{ARCHIVE_PREFIX}/volumes/pack-000000000000.tar.age",
        kind="pack",
        content=b"pack-plaintext",
    )
    manifest = _seed_encrypted_object(
        client,
        path=f"{ARCHIVE_PREFIX}/manifest.json.age",
        kind="manifest",
        content=b"{}",
    )
    identity = CollectionArchiveIdentity(objects=(pack, manifest))

    store.verify_collection_archive(collection_id=COLLECTION_ID, archive=identity)
    assert b"".join(store.iter_archive_object(collection_id=COLLECTION_ID, object=pack)) == (
        b"pack-plaintext"
    )
    assert (
        store.stored_archive_object_sha256(
            collection_id=COLLECTION_ID,
            object=pack,
        )
        == pack.stored_sha256
    )

    client.objects[pack.object_path]["Metadata"][PLAINTEXT_SHA256_METADATA] = "0" * 64
    with pytest.raises(ArchiveVerificationError):
        store.verify_collection_archive(collection_id=COLLECTION_ID, archive=identity)
    client.objects[pack.object_path]["Metadata"][PLAINTEXT_SHA256_METADATA] = pack.sha256

    store.delete_collection_archive(collection_id=COLLECTION_ID, objects=identity.objects)
    assert pack.object_path not in client.objects
    assert manifest.object_path not in client.objects


def test_attestation_artifacts_are_plaintext_and_replaceable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    store = _store(monkeypatch, tmp_path, client)

    receipt = store.publish_archive_attestation(
        collection_id=COLLECTION_ID,
        archive_storage_prefix=ARCHIVE_PREFIX,
        checksums=b"sum  manifest.json.age\n",
        signature=b"signature",
        proof=b"proof",
    )
    proof = receipt.require_object("signature-proof")
    artifact = store.read_archive_attestation_artifact(
        collection_id=COLLECTION_ID,
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
    assert artifact.content == b"proof"
    replaced = store.replace_archive_attestation_proof(
        collection_id=COLLECTION_ID,
        object=ArchiveObjectIdentity(
            object_id=proof.object_id,
            kind=proof.kind,
            object_path=proof.object_path,
            plaintext_bytes=proof.plaintext_bytes,
            stored_bytes=proof.stored_bytes,
            sha256=proof.sha256,
            stored_sha256=proof.stored_sha256,
        ),
        proof_bytes=b"mature-proof",
    )
    assert cast(bytes, client.objects[replaced.object_path]["Body"]) == b"mature-proof"


def test_incomplete_multipart_sweep_and_prefix_discard_are_exact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    store = _store(monkeypatch, tmp_path, client)
    stale = datetime(2026, 1, 1, tzinfo=UTC)
    recent = datetime(2026, 1, 3, tzinfo=UTC)
    client.uploads = {
        "stale": {"Key": f"{ARCHIVE_PREFIX}/volumes/a", "Initiated": stale},
        "recent": {"Key": f"{ARCHIVE_PREFIX}/volumes/b", "Initiated": recent},
        "other": {"Key": "unrelated/object", "Initiated": stale},
    }
    client.multipart_page_size = 1

    assert (
        store.abort_incomplete_multipart_uploads(initiated_before=datetime(2026, 1, 2, tzinfo=UTC))
        == 1
    )
    assert client.aborted_uploads == [(f"{ARCHIVE_PREFIX}/volumes/a", "stale")]

    client.objects[f"{ARCHIVE_PREFIX}/manifest.json.age"] = {"Body": b"x"}
    client.objects["unrelated/object"] = {"Body": b"y"}
    store.discard_collection_archive_upload(archive_storage_prefix=ARCHIVE_PREFIX)
    assert set(client.objects) == {"unrelated/object"}
    assert "recent" not in client.uploads
    assert "other" in client.uploads


def test_aws_deep_objects_are_restored_and_report_current_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    store = _store(
        monkeypatch,
        tmp_path,
        client,
        backend="aws",
        storage_class="DEEP_ARCHIVE",
    )
    deep = _seed_encrypted_object(
        client,
        path=f"{ARCHIVE_PREFIX}/volumes/segment-000000000000.bin.age",
        kind="segment",
        content=b"deep",
        storage_class="DEEP_ARCHIVE",
    )

    status = store.prepare_archive_objects_read(
        collection_id=COLLECTION_ID,
        objects=(deep,),
        retrieval_tier="bulk",
        hold_days=7,
        requested_at="2026-01-01T00:00:00Z",
        estimated_ready_at="2026-01-03T00:00:00Z",
    )
    assert status.state == "requested"
    assert client.restore_requests == [
        (
            deep.object_path,
            {"Days": 7, "GlacierJobParameters": {"Tier": "Bulk"}},
        )
    ]

    client.objects[deep.object_path]["Restore"] = (
        'ongoing-request="false", expiry-date="Fri, 09 Jan 2099 00:00:00 GMT"'
    )
    status = store.get_archive_objects_read_status(
        collection_id=COLLECTION_ID,
        objects=(deep,),
        requested_at="2026-01-01T00:00:00Z",
        estimated_ready_at="2026-01-03T00:00:00Z",
        estimated_expires_at=None,
    )
    assert status.state == "ready"


def test_archive_prefixes_are_opaque_and_owned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = _store(monkeypatch, tmp_path, _FakeS3Client())

    first = store.new_collection_archive_storage_prefix()
    second = store.new_collection_archive_storage_prefix()

    assert first.startswith("archive/archives/")
    assert second.startswith("archive/archives/")
    assert first != second
    assert len(first.rsplit("/", 1)[1]) == 32
    with pytest.raises(ValueError, match="outside the owned archive root"):
        store.discard_collection_archive_upload(archive_storage_prefix="unrelated")

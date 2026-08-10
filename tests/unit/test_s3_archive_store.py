from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from riverhog_age import decrypt_age_scrypt, encrypt_age_scrypt
from riverhog_core.archive_formats import archive_object_storage_format
from riverhog_core.ports.archive_store import (
    ArchiveObjectIdentity,
    ArchiveVerificationError,
    CollectionArchiveIdentity,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.stores.s3_archive_store import (
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


class _FakeCloudFrontResponse:
    is_success = True
    status_code = 200

    def __init__(self, content: bytes) -> None:
        self._content = content
        self.headers = {"content-length": str(len(content))}

    def __enter__(self) -> _FakeCloudFrontResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return

    def iter_bytes(self, *, chunk_size: int) -> Iterator[bytes]:
        for offset in range(0, len(self._content), chunk_size):
            yield self._content[offset : offset + chunk_size]


class _FakeCloudFrontClient:
    def __init__(self, content: bytes) -> None:
        self._content = content
        self.requests: list[str] = []

    def stream(self, method: str, url: str, *, headers: dict[str, str]):  # type: ignore[no-untyped-def]
        assert method == "GET"
        assert headers == {"Accept-Encoding": "identity"}
        self.requests.append(url)
        return _FakeCloudFrontResponse(self._content)


class _FakeCloudFrontSigner:
    def generate_presigned_url(self, url: str, *, date_less_than: datetime) -> str:
        assert date_less_than > datetime.now(UTC)
        return f"{url}&Signature=test"


class _FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.uploads: dict[str, dict[str, Any]] = {}
        self.versions: dict[str, set[str]] = {}
        self.version_objects: dict[tuple[str, str], dict[str, Any]] = {}
        self.version_sequence = 0
        self.restore_requests: list[tuple[str, str | None, object]] = []
        self.get_requests: list[tuple[str, str | None]] = []
        self.aborted_uploads: list[tuple[str, str]] = []
        self.multipart_page_size: int | None = None

    def head_object(
        self,
        *,
        Bucket: str,
        Key: str,
        VersionId: str | None = None,
    ) -> dict[str, Any]:
        del Bucket
        current = (
            self.version_objects.get((Key, VersionId))
            if VersionId is not None
            else self.objects.get(Key)
        )
        if current is None:
            raise _MissingObjectError
        return {key: value for key, value in current.items() if key != "Body"}

    def put_object(self, *, Bucket: str, Key: str, Body: object, **kwargs: Any) -> dict[str, str]:
        del Bucket
        body = bytes(Body) if isinstance(Body, (bytes, bytearray)) else b"".join(Body)  # type: ignore[arg-type]
        self.version_sequence += 1
        version_id = f"version-{self.version_sequence}"
        current = {
            "Body": body,
            "ContentLength": len(body),
            "LastModified": datetime(2026, 1, 1, tzinfo=UTC),
            "VersionId": version_id,
            **kwargs,
        }
        self.objects[Key] = current
        self.versions.setdefault(Key, set()).add(version_id)
        self.version_objects[(Key, version_id)] = current
        return {"VersionId": version_id}

    def get_object(
        self,
        *,
        Bucket: str,
        Key: str,
        VersionId: str | None = None,
    ) -> dict[str, object]:
        del Bucket
        self.get_requests.append((Key, VersionId))
        current = (
            self.version_objects[(Key, VersionId)] if VersionId is not None else self.objects[Key]
        )
        return {"Body": _FakeBody(cast(bytes, current["Body"]))}

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
                self.version_objects.pop((key, version_id), None)
                if self.objects.get(key, {}).get("VersionId") == version_id:
                    self.objects.pop(key, None)

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

    def restore_object(
        self,
        *,
        Bucket: str,
        Key: str,
        RestoreRequest: object,
        VersionId: str | None = None,
    ) -> None:
        del Bucket
        self.restore_requests.append((Key, VersionId, RestoreRequest))
        current = (
            self.version_objects[(Key, VersionId)] if VersionId is not None else self.objects[Key]
        )
        current["Restore"] = 'ongoing-request="true"'


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
    metadata = {
        "riverhog-format": archive_object_storage_format(kind),
        PLAINTEXT_BYTES_METADATA: str(len(content)),
    }
    if kind in {"manifest", "proof"}:
        metadata[PLAINTEXT_SHA256_METADATA] = plaintext_sha256
        metadata[STORED_SHA256_METADATA] = stored_sha256
    client.version_sequence += 1
    version_id = f"seed-{kind}-version-{client.version_sequence}"
    current = {
        "Body": ciphertext,
        "ContentLength": len(ciphertext),
        "LastModified": datetime(2026, 1, 1, tzinfo=UTC),
        "VersionId": version_id,
        "StorageClass": storage_class,
        "Metadata": metadata,
    }
    client.objects[path] = current
    client.versions.setdefault(path, set()).add(version_id)
    client.version_objects[(path, version_id)] = current
    return ArchiveObjectIdentity(
        object_id=kind,
        kind=kind,
        object_path=path,
        plaintext_bytes=len(content),
        stored_bytes=len(ciphertext),
        sha256=plaintext_sha256 if kind in {"manifest", "proof"} else None,
        stored_sha256=stored_sha256,
        version_id=version_id,
    )


def test_proof_replacement_retry_recovers_the_exact_provider_version(
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
    assert client.objects[proof_path]["Metadata"]["riverhog-format"] == (
        archive_object_storage_format("proof")
    )
    assert receipt.version_id == client.objects[proof_path]["VersionId"]
    persisted = store.read_archive_artifact(
        collection_id=COLLECTION_ID,
        object=ArchiveObjectIdentity(
            object_id=receipt.object_id,
            kind=receipt.kind,
            object_path=receipt.object_path,
            plaintext_bytes=receipt.plaintext_bytes,
            stored_bytes=receipt.stored_bytes,
            sha256=receipt.sha256,
            stored_sha256=receipt.stored_sha256,
            version_id=receipt.version_id,
        ),
    )
    assert persisted.content == b"new-proof"
    versions_after_replace = set(client.versions[proof_path])

    retry = store.replace_archive_proof(
        collection_id=COLLECTION_ID,
        object=old,
        proof_bytes=b"new-proof",
    )

    assert retry.version_id == receipt.version_id
    assert client.versions[proof_path] == versions_after_replace
    assert (proof_path, receipt.version_id) in client.get_requests


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

    repeated = store.publish_collection_metadata(
        collection_id=COLLECTION_ID,
        archive_storage_prefix=ARCHIVE_PREFIX,
        manifest=b'{"collection":1,"format":"riverhog-collection-metadata/v1"}',
    )

    assert repeated == receipt
    for path in ("archive/README.md", "archive/AGENTS.md"):
        assert len(client.versions[path]) == 1
    assert len(client.versions[receipt.object_path]) == 1


def test_archive_artifact_reads_the_cataloged_provider_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    store = _store(monkeypatch, tmp_path, client)
    path = f"{ARCHIVE_PREFIX}/manifest.json.age"
    old = _seed_encrypted_object(
        client,
        path=path,
        kind="manifest",
        content=b"old-manifest",
    )
    _seed_encrypted_object(
        client,
        path=path,
        kind="manifest",
        content=b"new-manifest",
    )

    artifact = store.read_archive_artifact(
        collection_id=COLLECTION_ID,
        object=old,
    )

    assert artifact.content == b"old-manifest"
    assert artifact.receipt.version_id == old.version_id


def test_cloudfront_read_requests_the_cataloged_provider_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    store = _store(monkeypatch, tmp_path, client)
    archived = _seed_encrypted_object(
        client,
        path=f"{ARCHIVE_PREFIX}/volumes/pack-000000000000.tar.age",
        kind="pack",
        content=b"archive-content",
    )
    archived = replace(archived, version_id="provider/version+id=")
    stored = cast(bytes, client.objects[archived.object_path]["Body"])
    cloudfront = _FakeCloudFrontClient(stored)
    unsafe_store = cast(Any, store)
    unsafe_store._store = replace(
        unsafe_store._store,
        cloudfront_base_url="https://archive.example.test",
    )
    unsafe_store._cloudfront_client = cloudfront
    unsafe_store._cloudfront_signer = _FakeCloudFrontSigner()

    assert b"".join(store._iter_cloudfront_stored_object(archived)) == stored
    assert cloudfront.requests == [
        "https://archive.example.test/"
        f"{archived.object_path}?versionId=provider%2Fversion%2Bid%3D&Signature=test"
    ]


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
    provenance_index = _seed_encrypted_object(
        client,
        path=f"{ARCHIVE_PREFIX}/provenance/index.json.age",
        kind="provenance-index",
        content=b"{}",
    )
    identity = CollectionArchiveIdentity(objects=(pack, manifest, provenance_index))

    store.verify_collection_archive(collection_id=COLLECTION_ID, archive=identity)
    assert b"".join(store.iter_archive_object(collection_id=COLLECTION_ID, object=pack)) == (
        b"pack-plaintext"
    )
    assert (
        store.read_archive_artifact(collection_id=COLLECTION_ID, object=manifest).content == b"{}"
    )
    assert (
        store.stored_archive_object_sha256(
            collection_id=COLLECTION_ID,
            object=pack,
        )
        == pack.stored_sha256
    )

    client.objects[pack.object_path]["Metadata"]["riverhog-format"] = "invalid"
    with pytest.raises(ArchiveVerificationError):
        store.verify_collection_archive(collection_id=COLLECTION_ID, archive=identity)
    client.objects[pack.object_path]["Metadata"]["riverhog-format"] = archive_object_storage_format(
        "pack"
    )

    store.delete_collection_archive(collection_id=COLLECTION_ID, objects=identity.objects)
    assert pack.object_path not in client.objects
    assert manifest.object_path not in client.objects
    assert provenance_index.object_path not in client.objects


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
            version_id=proof.version_id,
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
            version_id=proof.version_id,
        ),
        proof_bytes=b"mature-proof",
    )
    assert cast(bytes, client.objects[replaced.object_path]["Body"]) == b"mature-proof"
    assert client.objects[replaced.object_path]["Metadata"]["riverhog-format"] == (
        archive_object_storage_format("signature-proof")
    )
    assert proof.version_id is not None
    assert replaced.version_id == client.objects[replaced.object_path]["VersionId"]
    versions_after_replace = set(client.versions[replaced.object_path])

    retry = store.replace_archive_attestation_proof(
        collection_id=COLLECTION_ID,
        object=ArchiveObjectIdentity(
            object_id=proof.object_id,
            kind=proof.kind,
            object_path=proof.object_path,
            plaintext_bytes=proof.plaintext_bytes,
            stored_bytes=proof.stored_bytes,
            sha256=proof.sha256,
            stored_sha256=proof.stored_sha256,
            version_id=proof.version_id,
        ),
        proof_bytes=b"mature-proof",
    )

    assert retry.version_id == replaced.version_id
    assert client.versions[replaced.object_path] == versions_after_replace
    assert (replaced.object_path, replaced.version_id) in client.get_requests


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
            deep.version_id,
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

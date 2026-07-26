from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from riverhog_age import decrypt_age_scrypt
from riverhog_core.archive_objects import (
    STORED_OBJECT_LIMIT,
    CollectionArchive,
    CollectionArchiveDataObject,
    CollectionArchiveSourceFile,
    build_collection_archive,
)
from riverhog_core.catalog_db import initialize_db
from riverhog_core.ports.archive_store import (
    ArchiveMultipartUploadedPart,
    ArchiveMultipartUploadState,
    ArchiveMultipartUploadTracker,
    ArchiveObjectIdentity,
    ArchiveVerificationError,
    CollectionArchiveIdentity,
    CollectionArchiveUploadReceipt,
)
from riverhog_core.ports.retrieval_cache import RetrievalCacheReceipt
from riverhog_core.runtime_config import RetrievalCacheConfig, RuntimeConfig
from riverhog_core.services.download_allowances import SqlAlchemyDownloadAllowance
from riverhog_core.stores.s3_archive_store import (
    ArchiveMultipartTiming,
    S3ArchiveStore,
    _read_object_range,
)
from riverhog_protocol.errors import DownloadAllowanceExceeded

from tests.fixtures.crypto import FixtureProofStamper
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
        self.restore_requests: list[str] = []
        self.fail_after_parts: int | None = None
        self.uploaded_parts = 0
        self.next_upload = 1
        self.aborted_uploads: list[tuple[str, str]] = []
        self.multipart_page_size: int | None = None
        self.multipart_list_requests: list[dict[str, object]] = []
        self.versions: dict[str, set[str]] = {}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        _ = Bucket
        if Key not in self.objects:
            raise _MissingObjectError
        return {key: value for key, value in self.objects[Key].items() if key != "Body"}

    def put_object(self, *, Bucket: str, Key: str, Body: object, **kwargs: Any) -> dict[str, str]:
        _ = Bucket
        body = Body if isinstance(Body, bytes) else b"".join(cast(Iterator[bytes], Body))
        self.objects[Key] = {
            "Body": body,
            "ContentLength": len(body),
            "LastModified": datetime(2026, 1, 1, tzinfo=UTC),
            **kwargs,
        }
        return {}

    def create_multipart_upload(self, *, Bucket: str, Key: str, **kwargs: Any) -> dict[str, str]:
        _ = Bucket
        upload_id = f"upload-{self.next_upload}"
        self.next_upload += 1
        self.uploads[upload_id] = {
            "Key": Key,
            "Parts": {},
            "Args": kwargs,
            "Initiated": datetime(2026, 1, 1, tzinfo=UTC),
        }
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
        if self.fail_after_parts is not None and self.uploaded_parts >= self.fail_after_parts:
            self.fail_after_parts = None
            raise RuntimeError("synthetic multipart interruption")
        self.uploads[UploadId]["Parts"][PartNumber] = Body
        self.uploaded_parts += 1
        return {"ETag": f"etag-{UploadId}-{PartNumber}"}

    def list_parts(
        self,
        *,
        Bucket: str,
        Key: str,
        UploadId: str,
        PartNumberMarker: int = 0,
    ) -> dict[str, object]:
        _ = Bucket, Key
        return {
            "IsTruncated": False,
            "Parts": [
                {
                    "PartNumber": number,
                    "ETag": f"etag-{UploadId}-{number}",
                    "Size": len(body),
                }
                for number, body in sorted(self.uploads[UploadId]["Parts"].items())
                if number > PartNumberMarker
            ],
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
        body = b"".join(
            upload["Parts"][int(part["PartNumber"])] for part in MultipartUpload["Parts"]
        )
        self.objects[Key] = {
            "Body": body,
            "ContentLength": len(body),
            "LastModified": datetime(2026, 1, 1, tzinfo=UTC),
            **upload["Args"],
        }

    def list_multipart_uploads(
        self,
        *,
        Bucket: str,
        Prefix: str,
        KeyMarker: str = "",
        UploadIdMarker: str = "",
    ) -> dict[str, object]:
        self.multipart_list_requests.append(
            {
                "Bucket": Bucket,
                "Prefix": Prefix,
                "KeyMarker": KeyMarker,
                "UploadIdMarker": UploadIdMarker,
            }
        )
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

    def abort_multipart_upload(
        self,
        *,
        Bucket: str,
        Key: str,
        UploadId: str,
    ) -> None:
        _ = Bucket
        self.aborted_uploads.append((Key, UploadId))
        self.uploads.pop(UploadId, None)

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        _ = Bucket
        return {"Body": _FakeBody(cast(bytes, self.objects[Key]["Body"]))}

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        _ = Bucket
        self.objects.pop(Key, None)

    def get_paginator(self, name: str):
        assert name == "list_object_versions"
        client = self

        class Paginator:
            def paginate(self, *, Bucket: str, Prefix: str):
                _ = Bucket
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
        _ = Bucket
        for entry in cast(list[dict[str, str]], Delete["Objects"]):
            key = entry["Key"]
            version_id = entry.get("VersionId")
            if version_id is None:
                self.objects.pop(key, None)
                continue
            self.versions.get(key, set()).discard(version_id)

    def restore_object(self, *, Bucket: str, Key: str, RestoreRequest: object) -> None:
        _ = Bucket, RestoreRequest
        self.restore_requests.append(Key)
        self.objects[Key]["Restore"] = 'ongoing-request="true"'


class _TrackingS3Client(_FakeS3Client):
    def __init__(self) -> None:
        super().__init__()
        self._active_lock = threading.Lock()
        self.active_puts = 0
        self.max_active_puts = 0
        self.active_upload_parts = 0
        self.max_active_upload_parts = 0

    def put_object(self, *, Bucket: str, Key: str, Body: object, **kwargs: Any) -> None:
        with self._active_lock:
            self.active_puts += 1
            self.max_active_puts = max(self.max_active_puts, self.active_puts)
        try:
            time.sleep(0.05)
            super().put_object(Bucket=Bucket, Key=Key, Body=Body, **kwargs)
        finally:
            with self._active_lock:
                self.active_puts -= 1

    def upload_part(self, **kwargs: Any) -> dict[str, str]:
        with self._active_lock:
            self.active_upload_parts += 1
            self.max_active_upload_parts = max(
                self.max_active_upload_parts,
                self.active_upload_parts,
            )
        try:
            time.sleep(0.05)
            return super().upload_part(**kwargs)
        finally:
            with self._active_lock:
                self.active_upload_parts -= 1


class _Tracker(ArchiveMultipartUploadTracker):
    def __init__(self) -> None:
        self.states: dict[str, ArchiveMultipartUploadState] = {}
        self.parts: dict[str, list[ArchiveMultipartUploadedPart]] = {}
        self.cache_receipts: dict[str, RetrievalCacheReceipt] = {}

    def load_multipart_upload(self, **kwargs: object) -> ArchiveMultipartUploadState | None:
        object_id = str(kwargs["object_id"])
        state = self.states.get(object_id)
        return replace(state, parts=tuple(self.parts[object_id])) if state else None

    def save_multipart_upload(
        self, *, collection_id: int, state: ArchiveMultipartUploadState
    ) -> None:
        _ = collection_id
        self.states[state.object_id] = state
        self.parts[state.object_id] = []

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
        _ = collection_id, uploaded_bytes, uploaded_parts, total_parts
        self.parts[state.object_id].append(part)

    def clear_multipart_upload(self, *, collection_id: int, object_id: str, upload_id: str) -> None:
        _ = collection_id, upload_id
        self.states.pop(object_id, None)
        self.parts.pop(object_id, None)

    def load_ingestion_cache(
        self,
        *,
        collection_id: int,
        object_id: str,
    ) -> RetrievalCacheReceipt | None:
        _ = collection_id
        return self.cache_receipts.get(object_id)

    def save_ingestion_cache(
        self,
        *,
        collection_id: int,
        object_id: str,
        receipt: RetrievalCacheReceipt,
    ) -> None:
        _ = collection_id
        self.cache_receipts[object_id] = receipt


class _MemoryRetrievalCache:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str | None], bytes] = {}

    def put(
        self,
        *,
        source_store: str,
        collection_id: int,
        object_id: str,
        content: Iterator[bytes],
        content_length: int,
    ) -> RetrievalCacheReceipt:
        body = b"".join(content)
        assert len(body) == content_length
        object_path = f"cache/{source_store}/{collection_id}/{object_id}"
        version_id = hashlib.sha256(body).hexdigest()[:16]
        self.objects[(object_path, version_id)] = body
        return RetrievalCacheReceipt(
            object_path=object_path,
            version_id=version_id,
            stored_bytes=len(body),
            stored_sha256=hashlib.sha256(body).hexdigest(),
            cached_at="2026-07-18T00:00:00.000000Z",
            verified_at="2026-07-18T00:00:00.000000Z",
        )

    def iter_object(
        self,
        *,
        object_path: str,
        version_id: str | None,
        expected_bytes: int,
        expected_sha256: str,
    ) -> Iterator[bytes]:
        body = self.objects[(object_path, version_id)]
        assert len(body) == expected_bytes
        assert hashlib.sha256(body).hexdigest() == expected_sha256
        yield body

    def delete(self, *, object_path: str, version_id: str | None) -> None:
        del self.objects[(object_path, version_id)]


def _config(tmp_path: Path, **overrides: object) -> RuntimeConfig:
    config = RuntimeConfig(
        archive_passphrase="test-archive-passphrase",
        database_url=sqlite_url(tmp_path / "state.sqlite3"),
    )
    store_fields = {
        "archive_backend": "backend",
        "archive_storage_class": "storage_class",
        "cloudfront_base_url": "cloudfront_base_url",
        "cloudfront_public_key_id": "cloudfront_public_key_id",
        "cloudfront_private_key_path": "cloudfront_private_key_path",
        "monthly_download_allowance_bytes": "monthly_download_allowance_bytes",
        "download_safety_buffer_bytes": "download_safety_buffer_bytes",
    }
    store_overrides = {
        store_fields[name]: overrides.pop(name) for name in tuple(overrides) if name in store_fields
    }
    config = replace(config, **overrides)
    return replace(
        config,
        archive_stores={"deep": replace(config.archive_store("deep"), **store_overrides)},
    )


def _store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    client: _FakeS3Client,
    **overrides: object,
) -> S3ArchiveStore:
    monkeypatch.setattr(
        "riverhog_core.stores.s3_archive_store.create_archive_s3_client",
        lambda config, store: client,
    )
    config = _config(tmp_path, **overrides)
    return S3ArchiveStore(config, config.archive_store("deep"))


def _archive(content: bytes = b"hello"):
    return build_collection_archive(
        collection_id=COLLECTION_ID,
        files=(
            CollectionArchiveSourceFile(
                path="docs/readme.txt",
                content=content,
                sha256=hashlib.sha256(content).hexdigest(),
            ),
        ),
        max_plaintext_object_bytes=32 * 1024 * 1024,
        stamper=FixtureProofStamper(),
    )


def _multi_object_archive() -> CollectionArchive:
    contents = (b"first", b"second", b"third")
    objects = tuple(
        CollectionArchiveDataObject(
            object_id=f"data-{index:06d}",
            kind="pack",
            plaintext_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            placements=(),
            _chunks=lambda content=content: iter((content,)),
        )
        for index, content in enumerate(contents)
    )
    manifest = b"schema: test/v1\n"
    proof = b"test proof\n"
    return CollectionArchive(
        collection_id=COLLECTION_ID,
        files=(),
        data_objects=objects,
        manifest_bytes=manifest,
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        proof_bytes=proof,
        proof_sha256=hashlib.sha256(proof).hexdigest(),
    )


def test_archive_object_range_stops_reading_when_the_request_is_satisfied() -> None:
    chunks_read = 0

    def chunks() -> Iterator[bytes]:
        nonlocal chunks_read
        for chunk in (b"abcd", b"efgh"):
            chunks_read += 1
            yield chunk

    data = CollectionArchiveDataObject(
        object_id="data-000000",
        kind="file",
        plaintext_bytes=8,
        sha256=hashlib.sha256(b"abcdefgh").hexdigest(),
        placements=(),
        _chunks=chunks,
        _chunks_range=lambda _offset, _size: chunks(),
    )

    assert _read_object_range(data, 0, 4) == b"abcd"
    assert chunks_read == 1


def _identity(receipt: CollectionArchiveUploadReceipt) -> CollectionArchiveIdentity:
    objects = receipt.objects
    return CollectionArchiveIdentity(
        objects=tuple(
            ArchiveObjectIdentity(
                object_id=current.object_id,
                kind=current.kind,
                object_path=current.object_path,
                plaintext_bytes=current.plaintext_bytes,
                stored_bytes=current.stored_bytes,
                sha256=current.sha256,
            )
            for current in objects
        )
    )


def _cloudfront_private_key(path: Path) -> Path:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return path


def test_upload_encrypts_every_object_independently(monkeypatch, tmp_path: Path) -> None:
    client = _FakeS3Client()
    store = _store(monkeypatch, tmp_path, client)
    archive = _archive()

    receipt = store.upload_collection_archive(
        collection_id=COLLECTION_ID,
        archive=archive,
        archive_storage_prefix=ARCHIVE_PREFIX,
    )

    assert [current.object_id for current in receipt.objects] == [
        "data-000000",
        "manifest",
        "proof",
    ]
    assert receipt.require_object("data-000000").object_path.endswith("/objects/data-000000.age")
    for current in receipt.objects:
        plaintext = decrypt_age_scrypt(
            cast(bytes, client.objects[current.object_path]["Body"]),
            "test-archive-passphrase",
        )
        assert len(plaintext) == current.plaintext_bytes
        assert hashlib.sha256(plaintext).hexdigest() == current.sha256
    store.verify_collection_archive(collection_id=COLLECTION_ID, archive=_identity(receipt))


def test_archive_proof_replacement_is_encrypted_and_immediately_reverified(
    monkeypatch, tmp_path: Path
) -> None:
    client = _FakeS3Client()
    store = _store(monkeypatch, tmp_path, client)
    archive = _archive()
    uploaded = store.upload_collection_archive(
        collection_id=COLLECTION_ID,
        archive=archive,
        archive_storage_prefix=ARCHIVE_PREFIX,
    )
    manifest_identity = _identity(uploaded).require_object("manifest")
    proof_identity = _identity(uploaded).require_object("proof")

    manifest = store.read_archive_artifact(
        collection_id=COLLECTION_ID,
        object=manifest_identity,
    )
    proof = store.read_archive_artifact(
        collection_id=COLLECTION_ID,
        object=proof_identity,
    )

    assert manifest.content == archive.manifest_bytes
    assert proof.content == archive.proof_bytes

    completed_proof = archive.proof_bytes + b"matured\n"
    receipt = store.replace_archive_proof(
        collection_id=COLLECTION_ID,
        object=proof_identity,
        proof_bytes=completed_proof,
    )
    persisted = store.read_archive_artifact(
        collection_id=COLLECTION_ID,
        object=ArchiveObjectIdentity(
            object_id=receipt.object_id,
            kind=receipt.kind,
            object_path=receipt.object_path,
            plaintext_bytes=receipt.plaintext_bytes,
            stored_bytes=receipt.stored_bytes,
            sha256=receipt.sha256,
        ),
    )

    assert persisted.content == completed_proof
    assert persisted.receipt.storage_class == "STANDARD"
    assert (
        decrypt_age_scrypt(
            cast(bytes, client.objects[receipt.object_path]["Body"]),
            "test-archive-passphrase",
        )
        == completed_proof
    )


def test_collection_metadata_manifest_is_independently_encrypted(
    monkeypatch, tmp_path: Path
) -> None:
    client = _FakeS3Client()
    store = _store(monkeypatch, tmp_path, client)
    manifest = b"format: riverhog-collection-metadata/v1\ncollection: 1\ntags: [docs]\n"

    receipt = store.publish_collection_metadata(
        collection_id=COLLECTION_ID,
        archive_storage_prefix=ARCHIVE_PREFIX,
        manifest=manifest,
    )

    stored = client.objects[receipt.object_path]
    assert receipt.object_path == f"{ARCHIVE_PREFIX}/metadata.yml.age"
    assert (
        decrypt_age_scrypt(
            cast(bytes, stored["Body"]),
            "test-archive-passphrase",
        )
        == manifest
    )
    assert stored["Metadata"]["riverhog-collection-id"] == "1"
    assert stored["Metadata"]["collection-metadata-format"] == "riverhog-collection-metadata/v1"
    readme_key = next(key for key in client.objects if key.endswith("/README.md"))
    agents_key = next(key for key in client.objects if key.endswith("/AGENTS.md"))
    readme = cast(bytes, client.objects[readme_key]["Body"]).decode()
    agents = cast(bytes, client.objects[agents_key]["Body"]).decode()
    assert "archives/ARCHIVE_ID/metadata.yml.age" in readme
    assert "archives/ARCHIVE_ID/metadata.yml.age" in agents


def test_restore_required_upload_uses_exact_leased_cache_ciphertext(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    monkeypatch.setattr(
        "riverhog_core.stores.s3_archive_store.create_archive_s3_client",
        lambda config, store: client,
    )
    config = _config(tmp_path, archive_scrypt_work_factor=1)
    deep = replace(config.archive_store("deep"), read_mode="restore_required")
    config = replace(
        config,
        archive_stores={"deep": deep},
        retrieval_cache=RetrievalCacheConfig(
            endpoint_url="https://cache.example.invalid",
            region="us-east-1",
            bucket="retrieval-cache",
            access_key_id="key",
            secret_access_key="secret",
        ),
    )
    cache = _MemoryRetrievalCache()
    tracker = _Tracker()
    store = S3ArchiveStore(config, deep, retrieval_cache=cache)  # type: ignore[arg-type]
    archive = _archive()
    expected_plaintext = b"".join(archive.data_objects[0].iter_plaintext())

    receipt = store.upload_collection_archive(
        collection_id=COLLECTION_ID,
        archive=archive,
        archive_storage_prefix=ARCHIVE_PREFIX,
        multipart_tracker=tracker,
    )

    data = receipt.require_object("data-000000")
    assert data.ingestion_cache is not None
    cached = cache.objects[(data.ingestion_cache.object_path, data.ingestion_cache.version_id)]
    assert cast(bytes, client.objects[data.object_path]["Body"]) == cached
    assert decrypt_age_scrypt(cached, config.archive_passphrase) == expected_plaintext

    client.objects.pop(data.object_path)
    resumed = store.upload_collection_archive(
        collection_id=COLLECTION_ID,
        archive=archive,
        archive_storage_prefix=ARCHIVE_PREFIX,
        multipart_tracker=tracker,
    )
    assert cast(bytes, client.objects[data.object_path]["Body"]) == cached
    assert resumed.require_object("data-000000").ingestion_cache == data.ingestion_cache


def test_archive_download_uses_s3_when_cloudfront_is_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    store = _store(monkeypatch, tmp_path, client)
    archive = _archive()
    expected = b"".join(archive.data_objects[0].iter_plaintext())
    receipt = store.upload_collection_archive(
        collection_id=COLLECTION_ID,
        archive=archive,
        archive_storage_prefix=ARCHIVE_PREFIX,
    )

    restored = b"".join(
        store.iter_archive_object(
            collection_id=COLLECTION_ID,
            object=_identity(receipt).data_objects[0],
        )
    )

    assert restored == expected


def test_s3_download_uses_the_same_store_allowance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    initial = _store(monkeypatch, tmp_path, client)
    archive = _archive()
    receipt = initial.upload_collection_archive(
        collection_id=COLLECTION_ID,
        archive=archive,
        archive_storage_prefix=ARCHIVE_PREFIX,
    )
    data_object = _identity(receipt).data_objects[0]
    config = _config(
        tmp_path,
        monthly_download_allowance_bytes=data_object.stored_bytes,
    )
    initialize_db(config.database_url)
    allowance = SqlAlchemyDownloadAllowance(config)
    store = S3ArchiveStore(
        config,
        config.archive_store("deep"),
        download_allowance=allowance,
    )

    assert b"".join(
        store.iter_archive_object(collection_id=COLLECTION_ID, object=data_object)
    ) == b"".join(archive.data_objects[0].iter_plaintext())
    assert allowance.get_statuses()[0].accounted_bytes == data_object.stored_bytes


def test_configured_store_allowance_requires_its_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    monkeypatch.setattr(
        "riverhog_core.stores.s3_archive_store.create_archive_s3_client",
        lambda config, store: client,
    )
    config = _config(tmp_path, monthly_download_allowance_bytes=1_000)

    with pytest.raises(ValueError, match="requires its service"):
        S3ArchiveStore(config, config.archive_store("deep"))


def test_archive_download_uses_one_signed_cloudfront_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    s3_client = _FakeS3Client()
    requested_urls: list[httpx.URL] = []

    def handle_download(request: httpx.Request) -> httpx.Response:
        requested_urls.append(request.url)
        assert request.headers["Accept-Encoding"] == "identity"
        object_path = request.url.path.removeprefix("/")
        content = cast(bytes, s3_client.objects[object_path]["Body"])
        return httpx.Response(
            200,
            headers={"Content-Length": str(len(content))},
            content=content,
        )

    download_client = httpx.Client(transport=httpx.MockTransport(handle_download))
    monkeypatch.setattr(
        "riverhog_core.stores.s3_archive_store.httpx.Client",
        lambda **_kwargs: download_client,
    )
    store = _store(
        monkeypatch,
        tmp_path,
        s3_client,
        archive_backend="aws",
        archive_storage_class="STANDARD",
        cloudfront_base_url="https://archive.example.test",
        cloudfront_public_key_id="example-key-id",
        cloudfront_private_key_path=_cloudfront_private_key(tmp_path / "cloudfront.pem"),
    )
    archive = _archive()
    expected = b"".join(archive.data_objects[0].iter_plaintext())
    receipt = store.upload_collection_archive(
        collection_id=COLLECTION_ID,
        archive=archive,
        archive_storage_prefix=ARCHIVE_PREFIX,
    )
    data_object = _identity(receipt).data_objects[0]

    first = b"".join(store.iter_archive_object(collection_id=COLLECTION_ID, object=data_object))
    second = b"".join(store.iter_archive_object(collection_id=COLLECTION_ID, object=data_object))

    assert first == second == expected
    assert len(requested_urls) == 2
    for url in requested_urls:
        assert url.host == "archive.example.test"
        assert url.path == f"/{data_object.object_path}"
        assert set(url.params) == {"Expires", "Signature", "Key-Pair-Id"}
        assert url.params["Key-Pair-Id"] == "example-key-id"


def test_cloudfront_download_is_accounted_before_another_object_is_opened(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    s3_client = _FakeS3Client()
    requested_urls: list[httpx.URL] = []

    def handle_download(request: httpx.Request) -> httpx.Response:
        requested_urls.append(request.url)
        object_path = request.url.path.removeprefix("/")
        content = cast(bytes, s3_client.objects[object_path]["Body"])
        return httpx.Response(
            200,
            headers={"Content-Length": str(len(content))},
            content=content,
        )

    download_client = httpx.Client(transport=httpx.MockTransport(handle_download))
    monkeypatch.setattr(
        "riverhog_core.stores.s3_archive_store.httpx.Client",
        lambda **_kwargs: download_client,
    )
    private_key = _cloudfront_private_key(tmp_path / "cloudfront.pem")
    initial = _store(
        monkeypatch,
        tmp_path,
        s3_client,
        archive_backend="aws",
        archive_storage_class="STANDARD",
        cloudfront_base_url="https://archive.example.test",
        cloudfront_public_key_id="example-key-id",
        cloudfront_private_key_path=private_key,
    )
    archive = _archive()
    receipt = initial.upload_collection_archive(
        collection_id=COLLECTION_ID,
        archive=archive,
        archive_storage_prefix=ARCHIVE_PREFIX,
    )
    data_object = _identity(receipt).data_objects[0]
    config = _config(
        tmp_path,
        archive_backend="aws",
        archive_storage_class="STANDARD",
        cloudfront_base_url="https://archive.example.test",
        cloudfront_public_key_id="example-key-id",
        cloudfront_private_key_path=private_key,
        monthly_download_allowance_bytes=data_object.stored_bytes,
    )
    initialize_db(config.database_url)
    allowance = SqlAlchemyDownloadAllowance(config)
    store = S3ArchiveStore(
        config,
        config.archive_store("deep"),
        download_allowance=allowance,
    )

    assert b"".join(
        store.iter_archive_object(collection_id=COLLECTION_ID, object=data_object)
    ) == b"".join(archive.data_objects[0].iter_plaintext())
    status = allowance.get_statuses()[0]
    assert status.accounted_bytes == data_object.stored_bytes
    assert status.remaining_bytes == 0

    with pytest.raises(DownloadAllowanceExceeded):
        b"".join(store.iter_archive_object(collection_id=COLLECTION_ID, object=data_object))
    assert len(requested_urls) == 1


def test_configured_cloudfront_download_failure_does_not_fall_back_to_s3(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    s3_client = _FakeS3Client()
    download_client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(503))
    )
    monkeypatch.setattr(
        "riverhog_core.stores.s3_archive_store.httpx.Client",
        lambda **_kwargs: download_client,
    )
    store = _store(
        monkeypatch,
        tmp_path,
        s3_client,
        archive_backend="aws",
        archive_storage_class="STANDARD",
        cloudfront_base_url="https://archive.example.test",
        cloudfront_public_key_id="example-key-id",
        cloudfront_private_key_path=_cloudfront_private_key(tmp_path / "cloudfront.pem"),
    )
    receipt = store.upload_collection_archive(
        collection_id=COLLECTION_ID,
        archive=_archive(),
        archive_storage_prefix=ARCHIVE_PREFIX,
    )
    data_object = _identity(receipt).data_objects[0]
    monkeypatch.setattr(
        s3_client,
        "get_object",
        lambda **_kwargs: pytest.fail("configured CloudFront downloads must not use S3 GET"),
    )

    with pytest.raises(RuntimeError, match="CloudFront archive download failed with HTTP 503"):
        b"".join(store.iter_archive_object(collection_id=COLLECTION_ID, object=data_object))


@pytest.mark.parametrize(
    ("multipart_part_bytes", "expected_max_active_puts"),
    ((64 * 1024 * 1024, 3), (1, 1)),
)
def test_upload_concurrently_submits_only_one_part_sized_objects(
    monkeypatch,
    tmp_path: Path,
    multipart_part_bytes: int,
    expected_max_active_puts: int,
) -> None:
    client = _TrackingS3Client()
    store = _store(
        monkeypatch,
        tmp_path,
        client,
        archive_multipart_part_bytes=multipart_part_bytes,
        archive_object_concurrency=3,
        archive_scrypt_work_factor=1,
    )

    receipt = store.upload_collection_archive(
        collection_id=COLLECTION_ID,
        archive=_multi_object_archive(),
        archive_storage_prefix=ARCHIVE_PREFIX,
    )

    assert [current.object_id for current in receipt.objects] == [
        "data-000000",
        "data-000001",
        "data-000002",
        "manifest",
        "proof",
    ]
    assert client.max_active_puts == expected_max_active_puts


def test_upload_resumes_each_data_object_independently(monkeypatch, tmp_path: Path) -> None:
    client = _FakeS3Client()
    store = _store(monkeypatch, tmp_path, client, archive_multipart_part_bytes=5 * 1024 * 1024)
    archive = _archive(b"x" * (8 * 1024 * 1024))
    tracker = _Tracker()
    client.fail_after_parts = 1

    with pytest.raises(RuntimeError, match="interruption"):
        store.upload_collection_archive(
            collection_id=COLLECTION_ID,
            archive=archive,
            archive_storage_prefix=ARCHIVE_PREFIX,
            multipart_tracker=tracker,
        )
    receipt = store.upload_collection_archive(
        collection_id=COLLECTION_ID,
        archive=archive,
        archive_storage_prefix=ARCHIVE_PREFIX,
        multipart_tracker=tracker,
    )

    assert not tracker.states
    data = receipt.require_object("data-000000")
    assert decrypt_age_scrypt(
        cast(bytes, client.objects[data.object_path]["Body"]),
        "test-archive-passphrase",
    ) == b"".join(archive.data_objects[0].iter_plaintext())


def test_encrypted_multipart_upload_pipelines_bounded_provider_requests(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = _TrackingS3Client()
    timings: list[ArchiveMultipartTiming] = []
    monkeypatch.setattr(
        "riverhog_core.stores.s3_archive_store.create_archive_s3_client",
        lambda config, store: client,
    )
    config = _config(
        tmp_path,
        archive_multipart_part_bytes=5 * 1024 * 1024,
        archive_multipart_concurrency=3,
        archive_scrypt_work_factor=1,
    )
    store = S3ArchiveStore(
        config,
        config.archive_store("deep"),
        multipart_timing_observer=timings.append,
    )

    receipt = store.upload_collection_archive(
        collection_id=COLLECTION_ID,
        archive=_archive(b"x" * (16 * 1024 * 1024)),
        archive_storage_prefix=ARCHIVE_PREFIX,
        multipart_tracker=_Tracker(),
    )

    assert client.max_active_upload_parts == 3
    assert timings and timings[0].object_id == "data-000000"
    assert timings[0].concurrency == 3
    assert timings[0].parts == 4
    assert timings[0].preparation_seconds >= 0
    assert timings[0].upload_request_seconds >= 0
    assert timings[0].elapsed_seconds >= 0
    encrypted = cast(bytes, client.objects[receipt.objects[0].object_path]["Body"])
    assert decrypt_age_scrypt(encrypted, "test-archive-passphrase") == b"x" * (16 * 1024 * 1024)


def test_incomplete_multipart_sweep_aborts_only_stale_owned_uploads(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    client.multipart_page_size = 2
    client.uploads = {
        "old-a": {
            "Key": "archive/archives/a/objects/data-000000.age",
            "Initiated": datetime(2026, 7, 1, tzinfo=UTC),
        },
        "old-b": {
            "Key": "archive/archives/b/objects/data-000000.age",
            "Initiated": datetime(2026, 7, 2, tzinfo=UTC),
        },
        "cutoff": {
            "Key": "archive/archives/c/objects/data-000000.age",
            "Initiated": datetime(2026, 7, 10, tzinfo=UTC),
        },
        "fresh": {
            "Key": "archive/archives/d/objects/data-000000.age",
            "Initiated": datetime(2026, 7, 11, tzinfo=UTC),
        },
        "other": {
            "Key": "unrelated/archive-object.age",
            "Initiated": datetime(2026, 7, 1, tzinfo=UTC),
        },
    }
    store = _store(monkeypatch, tmp_path, client)

    aborted = store.abort_incomplete_multipart_uploads(
        initiated_before=datetime(2026, 7, 10, tzinfo=UTC)
    )

    assert aborted == 2
    assert client.aborted_uploads == [
        ("archive/archives/a/objects/data-000000.age", "old-a"),
        ("archive/archives/b/objects/data-000000.age", "old-b"),
    ]
    assert set(client.uploads) == {"cutoff", "fresh", "other"}
    assert len(client.multipart_list_requests) == 2
    assert {str(request["Prefix"]) for request in client.multipart_list_requests} == {
        "archive/archives/"
    }


def test_read_preparation_requests_only_selected_deep_objects(monkeypatch, tmp_path: Path) -> None:
    client = _FakeS3Client()
    store = _store(
        monkeypatch,
        tmp_path,
        client,
        archive_backend="aws",
        archive_storage_class="DEEP_ARCHIVE",
    )
    receipt = store.upload_collection_archive(
        collection_id=COLLECTION_ID,
        archive=_archive(),
        archive_storage_prefix=ARCHIVE_PREFIX,
    )
    data = _identity(receipt).data_objects

    status = store.prepare_archive_objects_read(
        collection_id=COLLECTION_ID,
        objects=data,
        retrieval_tier="bulk",
        hold_days=1,
        requested_at="2026-01-01T00:00:00.000000Z",
        estimated_ready_at="2026-01-01T12:00:00.000000Z",
    )

    assert status.state == "requested"
    assert client.restore_requests == [data[0].object_path]


def test_verification_and_deletion_cover_the_complete_object_set(
    monkeypatch, tmp_path: Path
) -> None:
    client = _FakeS3Client()
    store = _store(monkeypatch, tmp_path, client)
    receipt = store.upload_collection_archive(
        collection_id=COLLECTION_ID,
        archive=_archive(),
        archive_storage_prefix=ARCHIVE_PREFIX,
    )
    identity = _identity(receipt)
    client.objects[identity.objects[0].object_path]["ContentLength"] += 1
    with pytest.raises(ArchiveVerificationError):
        store.verify_collection_archive(collection_id=COLLECTION_ID, archive=identity)
    client.objects[identity.objects[0].object_path]["ContentLength"] -= 1
    for archive_object in identity.objects:
        client.versions[archive_object.object_path] = {"historic", "current"}
    client.versions[f"{identity.objects[0].object_path}.neighbor"] = {"untouched"}

    store.delete_collection_archive(collection_id=COLLECTION_ID, objects=identity.objects)
    assert not client.objects
    assert all(
        not client.versions[archive_object.object_path] for archive_object in identity.objects
    )
    assert client.versions[f"{identity.objects[0].object_path}.neighbor"] == {"untouched"}


def test_store_plaintext_limit_reserves_age_framing(monkeypatch, tmp_path: Path) -> None:
    store = _store(monkeypatch, tmp_path, _FakeS3Client())
    assert 0 < store.max_plaintext_object_bytes() < STORED_OBJECT_LIMIT

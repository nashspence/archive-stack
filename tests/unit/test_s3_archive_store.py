from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from riverhog_age import decrypt_age_scrypt
from riverhog_core.archive_objects import (
    STORED_OBJECT_LIMIT,
    CollectionArchive,
    CollectionArchiveDataObject,
    CollectionArchiveSourceFile,
    build_collection_archive,
)
from riverhog_core.ports.archive_store import (
    ArchiveMultipartUploadedPart,
    ArchiveMultipartUploadState,
    ArchiveMultipartUploadTracker,
    ArchiveObjectIdentity,
    ArchiveVerificationError,
    CollectionArchiveIdentity,
    CollectionArchiveUploadReceipt,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.stores.s3_archive_store import ArchiveMultipartTiming, S3ArchiveStore
from tests.fixtures.crypto import FixtureProofStamper
from tests.unit.db_helpers import sqlite_url

COLLECTION_ID = "2025/20250102T030405Z__docs"
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

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        _ = Bucket
        if Key not in self.objects:
            raise _MissingObjectError
        return {key: value for key, value in self.objects[Key].items() if key != "Body"}

    def put_object(self, *, Bucket: str, Key: str, Body: object, **kwargs: Any) -> None:
        _ = Bucket
        body = Body if isinstance(Body, bytes) else b"".join(cast(Iterator[bytes], Body))
        self.objects[Key] = {
            "Body": body,
            "ContentLength": len(body),
            "LastModified": datetime(2026, 1, 1, tzinfo=UTC),
            **kwargs,
        }

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

    def load_multipart_upload(self, **kwargs: object) -> ArchiveMultipartUploadState | None:
        object_id = str(kwargs["object_id"])
        state = self.states.get(object_id)
        return replace(state, parts=tuple(self.parts[object_id])) if state else None

    def save_multipart_upload(
        self, *, collection_id: str, state: ArchiveMultipartUploadState
    ) -> None:
        _ = collection_id
        self.states[state.object_id] = state
        self.parts[state.object_id] = []

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
        self.parts[state.object_id].append(part)

    def clear_multipart_upload(self, *, collection_id: str, object_id: str, upload_id: str) -> None:
        _ = collection_id, upload_id
        self.states.pop(object_id, None)
        self.parts.pop(object_id, None)


def _config(tmp_path: Path, **overrides: object) -> RuntimeConfig:
    config = RuntimeConfig(
        hot_store_endpoint_url="http://example.invalid:9000",
        hot_store_region="us-east-1",
        hot_store_bucket="riverhog",
        hot_store_access_key_id="test-access",
        hot_store_secret_access_key="test-secret",
        hot_store_force_path_style=True,
        archive_passphrase="test-archive-passphrase",
        database_url=sqlite_url(tmp_path / "state.sqlite3"),
    )
    store_fields = {
        "archive_backend": "backend",
        "archive_storage_class": "storage_class",
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
    assert decrypt_age_scrypt(encrypted, "test-archive-passphrase") == b"x" * (
        16 * 1024 * 1024
    )


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

    store.delete_collection_archive(collection_id=COLLECTION_ID, objects=identity.objects)
    assert not client.objects


def test_store_plaintext_limit_reserves_age_framing(monkeypatch, tmp_path: Path) -> None:
    store = _store(monkeypatch, tmp_path, _FakeS3Client())
    assert 0 < store.max_plaintext_object_bytes() < STORED_OBJECT_LIMIT

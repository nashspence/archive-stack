from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveCopyRecord,
    CollectionArchiveObjectRecord,
    CollectionFileRecord,
    CollectionRecord,
    CollectionUploadFileRecord,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.collections import SqlAlchemyCollectionService
from riverhog_protocol.errors import Conflict, NotFound
from sqlalchemy import select

from tests.unit.db_helpers import sqlite_url


class UploadStoreStub:
    def __init__(self) -> None:
        self.lengths: list[int] = []

    def create_upload(self, target_path: str, length: int) -> str:
        self.lengths.append(length)
        return f"http://tusd.invalid/files/{target_path.rsplit('/', 1)[-1]}"

    def get_offset(self, tus_url: str) -> int:
        return 0

    def cancel_upload(self, tus_url: str) -> None:
        pass

    def delete_target(self, target_path: str) -> None:
        pass


class MissingTargetUploadStore(UploadStoreStub):
    def __init__(self) -> None:
        super().__init__()
        self.canceled: list[str] = []
        self.deleted: list[str] = []

    def iter_target(
        self,
        target_path: str,
        *,
        offset: int = 0,
        size: int | None = None,
    ) -> Iterator[bytes]:
        raise NotFound(f"upload target not found: {target_path}")

    def cancel_upload(self, tus_url: str) -> None:
        self.canceled.append(tus_url)

    def delete_target(self, target_path: str) -> None:
        self.deleted.append(target_path)


class ConcurrentCancelUploadStore(UploadStoreStub):
    def __init__(self, expected_concurrency: int) -> None:
        super().__init__()
        self.expected_concurrency = expected_concurrency
        self.canceled: list[str] = []
        self.deleted: list[str] = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.all_active = threading.Event()

    def cancel_upload(self, tus_url: str) -> None:
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.active == self.expected_concurrency:
                self.all_active.set()
        if not self.all_active.wait(timeout=2):
            raise AssertionError("upload cancellation did not run concurrently")
        with self.lock:
            self.canceled.append(tus_url)
            self.active -= 1

    def delete_target(self, target_path: str) -> None:
        with self.lock:
            self.deleted.append(target_path)


class ConcurrentVerifyUploadStore(UploadStoreStub):
    def __init__(self, expected_concurrency: int) -> None:
        super().__init__()
        self.expected_concurrency = expected_concurrency
        self.verified: list[str] = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.all_active = threading.Event()

    def iter_target(
        self,
        target_path: str,
        *,
        offset: int = 0,
        size: int | None = None,
    ) -> Iterator[bytes]:
        assert offset == 0
        assert size == 1
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.active == self.expected_concurrency:
                self.all_active.set()
        if not self.all_active.wait(timeout=2):
            raise AssertionError("finalized-target verification did not run concurrently")
        with self.lock:
            self.verified.append(target_path)
            self.active -= 1
        yield b"x"


def _service(path: Path, store: UploadStoreStub | None = None) -> SqlAlchemyCollectionService:
    return SqlAlchemyCollectionService(
        RuntimeConfig(database_url=sqlite_url(path)),
        store or UploadStoreStub(),
    )


def _archive_copy(store: str, *, stored_bytes: int) -> CollectionArchiveCopyRecord:
    collection_id = "2025/20250101T000000Z__alpha"
    verified_at = "2026-07-14T00:00:00Z"
    prefix = f"collections/alpha/{store}"
    copy = CollectionArchiveCopyRecord(
        collection_id=collection_id,
        store=store,
        state="uploaded",
        archive_storage_prefix=prefix,
        backend="s3",
        storage_class="STANDARD",
        last_uploaded_at=verified_at,
        last_verified_at=verified_at,
    )
    for order, (object_id, kind, size) in enumerate(
        (
            ("data-000000", "file", stored_bytes),
            ("manifest", "manifest", 1),
            ("proof", "proof", 1),
        )
    ):
        copy.objects.append(
            CollectionArchiveObjectRecord(
                collection_id=collection_id,
                store=store,
                object_id=object_id,
                object_order=order,
                kind=kind,
                object_path=f"{prefix}/{object_id}.age",
                plaintext_bytes=max(0, size - 1),
                stored_bytes=size,
                sha256="c" * 64,
                backend="s3",
                storage_class="STANDARD",
                uploaded_at=verified_at,
                verified_at=verified_at,
            )
        )
    return copy


def _seed(path: Path) -> None:
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        for collection_id, size in (
            ("2025/20250101T000000Z__alpha", 10),
            ("2025/20250102T000000Z__beta", 20),
        ):
            session.add(CollectionRecord(id=collection_id, manifest_etag="0" * 64))
            session.add(
                CollectionFileRecord(
                    collection_id=collection_id,
                    path="file.txt",
                    bytes=size,
                    sha256=("a" if collection_id.endswith("__alpha") else "b") * 64,
                )
            )
        session.add(_archive_copy("deep", stored_bytes=14))


def test_collection_summary_reports_each_archive_copy(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    _seed(path)
    with session_scope(make_session_factory(sqlite_url(path))) as session:
        session.add(_archive_copy("b2", stored_bytes=15))

    summary = _service(path).get("2025/20250101T000000Z__alpha")

    assert summary.files == 1
    assert summary.bytes == 10
    assert [copy.store for copy in summary.archive_copies] == ["b2", "deep"]


def test_collection_list_aggregates_and_sorts_in_the_database(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    _seed(path)

    page = _service(path).list(
        page=1,
        per_page=25,
        q=None,
        sort="bytes",
        order="desc",
    )

    assert [(str(item.id), item.files, item.bytes) for item in page.collections] == [
        ("2025/20250102T000000Z__beta", 1, 20),
        ("2025/20250101T000000Z__alpha", 1, 10),
    ]


def test_file_preflight_returns_random_ingress_secret_and_persists_only_envelope(
    tmp_path: Path,
) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    upload_store = UploadStoreStub()
    service = _service(path, upload_store)
    created = service.create_or_resume_upload_session(
        upload_slug="encrypted",
        upload_timestamp="20250103T000000Z",
    )
    collection_id = str(created["collection_id"])
    service.register_upload_session_file(
        collection_id,
        {"path": "one.txt", "bytes": 10, "sha256": "a" * 64},
    )

    preflight = service.create_or_resume_file_upload(collection_id, "one.txt")
    repeated = service.create_or_resume_file_upload(collection_id, "one.txt")

    encryption = preflight["encryption"]
    assert isinstance(encryption, dict)
    assert encryption["format"] == "age-v1-scrypt-resumable"
    assert encryption["plaintext_bytes"] == 10
    assert preflight["length"] == encryption["ciphertext_bytes"]
    assert int(preflight["length"]) > 10
    assert repeated["encryption"] == encryption
    assert upload_store.lengths == [preflight["length"]]

    with session_scope(make_session_factory(sqlite_url(path))) as session:
        record = session.get(CollectionUploadFileRecord, (collection_id, "one.txt"))
        assert record is not None
        assert record.ingress_secret_envelope.startswith("v1.")
        assert record.ingress_secret_envelope != encryption["passphrase"]
        assert encryption["passphrase"] not in record.ingress_state_json


def test_each_file_registration_has_a_unique_ingress_object_identity(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    service = _service(path)
    upload_ids: list[str] = []

    for _ in range(2):
        created = service.create_or_resume_upload_session(
            upload_slug="repeat",
            upload_timestamp="20250103T000000Z",
        )
        collection_id = str(created["collection_id"])
        service.register_upload_session_file(
            collection_id,
            {"path": "one.txt", "bytes": 10, "sha256": "a" * 64},
        )
        with session_scope(make_session_factory(sqlite_url(path))) as session:
            record = session.get(CollectionUploadFileRecord, (collection_id, "one.txt"))
            assert record is not None
            upload_ids.append(record.ingress_upload_id)
        service.cancel_upload_session(collection_id)

    assert len(set(upload_ids)) == 2


def test_collection_upload_cancellation_cleans_targets_concurrently(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    file_count = 8
    worker_count = 4
    upload_store = ConcurrentCancelUploadStore(worker_count)
    service = _service(path, upload_store)
    created = service.create_or_resume_upload_session(
        upload_slug="cancel-me",
        upload_timestamp="20250103T000000Z",
    )
    collection_id = str(created["collection_id"])
    for index in range(file_count):
        file_path = f"file-{index}.txt"
        service.register_upload_session_file(
            collection_id,
            {"path": file_path, "bytes": 10, "sha256": f"{index:x}" * 64},
        )
        service.create_or_resume_file_upload(collection_id, file_path)

    canceled = service.cancel_upload_session(collection_id)

    assert canceled["state"] == "canceled"
    assert upload_store.max_active == worker_count
    assert len(upload_store.canceled) == file_count
    assert len(upload_store.deleted) == file_count


def test_collection_upload_completion_verifies_targets_with_bounded_concurrency(
    tmp_path: Path,
) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    file_count = 8
    worker_count = 4
    upload_store = ConcurrentVerifyUploadStore(worker_count)
    service = _service(path, upload_store)
    created = service.create_or_resume_upload_session(
        upload_slug="verify-me",
        upload_timestamp="20250103T000000Z",
    )
    collection_id = str(created["collection_id"])
    for index in range(file_count):
        file_path = f"file-{index}.txt"
        service.register_upload_session_file(
            collection_id,
            {"path": file_path, "bytes": 10, "sha256": f"{index:x}" * 64},
        )
        service.create_or_resume_file_upload(collection_id, file_path)

    with session_scope(make_session_factory(sqlite_url(path))) as session:
        records = list(
            session.scalars(
                select(CollectionUploadFileRecord).where(
                    CollectionUploadFileRecord.collection_id == collection_id
                )
            )
        )
        for record in records:
            record.ingress_uploaded_bytes = record.ingress_bytes
            record.upload_expires_at = None

    completed = service.complete_upload_session(collection_id)

    assert completed["state"] == "archiving"
    assert upload_store.max_active == worker_count
    assert len(upload_store.verified) == file_count


def test_collection_upload_completion_persists_missing_target_state(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    upload_store = MissingTargetUploadStore()
    service = _service(path, upload_store)
    created = service.create_or_resume_upload_session(
        upload_slug="missing-target",
        upload_timestamp="20250103T000000Z",
    )
    collection_id = str(created["collection_id"])
    service.register_upload_session_file(
        collection_id,
        {"path": "one.txt", "bytes": 10, "sha256": "a" * 64},
    )
    service.create_or_resume_file_upload(collection_id, "one.txt")

    with session_scope(make_session_factory(sqlite_url(path))) as session:
        record = session.get(CollectionUploadFileRecord, (collection_id, "one.txt"))
        assert record is not None
        record.ingress_uploaded_bytes = record.ingress_bytes
        record.upload_expires_at = None

    with pytest.raises(Conflict, match="still has missing file bytes"):
        service.complete_upload_session(collection_id)

    with session_scope(make_session_factory(sqlite_url(path))) as session:
        record = session.get(CollectionUploadFileRecord, (collection_id, "one.txt"))
        assert record is not None
        assert record.ingress_uploaded_bytes == 0
        assert record.tus_url is None
        assert record.upload_expires_at is None
    assert len(upload_store.canceled) == 1
    assert len(upload_store.deleted) == 1

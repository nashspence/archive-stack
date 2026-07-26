from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from riverhog_core.app_permissions import (
    CATALOG_READ,
    COLLECTIONS_CREATE,
    ApplicationAccess,
    ApplicationPrincipal,
)
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveCopyRecord,
    CollectionArchiveObjectRecord,
    CollectionFileRecord,
    CollectionRecord,
    CollectionTagRecord,
    CollectionUploadFileRecord,
    TagRecord,
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


def _seed_tag(path: Path, tag: str) -> None:
    with session_scope(make_session_factory(sqlite_url(path))) as session:
        session.add(
            TagRecord(
                id=tag,
                created_by_app="fixture",
                created_at="2026-01-01T00:00:00.000000Z",
            )
        )


def _archive_copy(store: str, *, stored_bytes: int) -> CollectionArchiveCopyRecord:
    collection_id = 1
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
                stored_sha256="c" * 64,
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
        session.add_all(
            [
                TagRecord(
                    id=tag,
                    created_by_app="fixture",
                    created_at="2026-01-01T00:00:00.000000Z",
                )
                for tag in ("alpha", "beta")
            ]
        )
        for collection_id, tag, size in (
            (1, "alpha", 10),
            (2, "beta", 20),
        ):
            session.add(
                CollectionRecord(
                    id=collection_id,
                    creation_idempotency_key=f"fixture-{collection_id}",
                    content_etag="0" * 64,
                    record_etag="1" * 64,
                    metadata_revision=1,
                    metadata_updated_at="2026-01-01T00:00:00.000000Z",
                    created_by_app="fixture",
                    created_at="2026-01-01T00:00:00.000000Z",
                )
            )
            session.add(
                CollectionTagRecord(
                    collection_id=collection_id,
                    tag_id=tag,
                    assigned_by_app="fixture",
                    assigned_at="2026-01-01T00:00:00.000000Z",
                )
            )
            session.add(
                CollectionFileRecord(
                    collection_id=collection_id,
                    path="file.txt",
                    bytes=size,
                    sha256=("a" if collection_id == 1 else "b") * 64,
                )
            )
        session.add(_archive_copy("deep", stored_bytes=14))


def test_collection_summary_reports_each_archive_copy(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    _seed(path)
    with session_scope(make_session_factory(sqlite_url(path))) as session:
        session.add(_archive_copy("b2", stored_bytes=15))

    summary = _service(path).get("1")

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
        ("2", 1, 20),
        ("1", 1, 10),
    ]


def test_collection_list_and_get_apply_exact_database_grants(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    _seed(path)
    principal = ApplicationPrincipal(
        app="reader",
        key_id="reader-key",
        access=frozenset({ApplicationAccess(CATALOG_READ, "collection:2")}),
    )

    page = _service(path).list(
        page=1,
        per_page=25,
        q=None,
        sort="id",
        order="asc",
        principal=principal,
    )

    assert page.total == 1
    assert [str(item.id) for item in page.collections] == ["2"]
    assert _service(path).get("2", principal=principal).bytes == 20
    with pytest.raises(NotFound):
        _service(path).get("1", principal=principal)


def test_finalized_upload_status_remains_visible_to_creating_application(
    tmp_path: Path,
) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    _seed(path)
    service = _service(path)
    creator = ApplicationPrincipal(
        app="fixture",
        key_id="replacement-key",
        access=frozenset({ApplicationAccess(COLLECTIONS_CREATE, "tag:alpha")}),
    )

    service.require_upload_access(1, creator)
    payload = service.get_upload(1)

    assert payload["state"] == "finalized"
    assert payload["collection_id"] == 1
    assert payload["tags"] == ["alpha"]
    assert payload["collection"] is not None


def test_finalized_upload_status_does_not_grant_cross_application_visibility(
    tmp_path: Path,
) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    _seed(path)
    principal = ApplicationPrincipal(
        app="other",
        key_id="other-key",
        access=frozenset({ApplicationAccess(COLLECTIONS_CREATE, "tag:alpha")}),
    )

    with pytest.raises(NotFound, match="collection upload not found"):
        _service(path).require_upload_access(1, principal)


def test_idempotency_key_with_different_manifest_conflicts(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    _seed_tag(path, "photos")
    service = _service(path)

    first = service.create_or_resume_upload(
        idempotency_key="photos-upload",
        tags=["photos"],
        files=[{"path": "one.jpg", "bytes": 1, "sha256": "a" * 64}],
    )

    assert first["collection_id"] == 1
    with pytest.raises(Conflict, match="files are immutable"):
        service.create_or_resume_upload(
            idempotency_key="photos-upload",
            tags=["photos"],
            files=[{"path": "two.jpg", "bytes": 1, "sha256": "b" * 64}],
        )


def test_collection_upload_requires_registered_tags(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))

    with pytest.raises(NotFound, match="tag not found: photos"):
        _service(path).create_or_resume_upload_session(
            idempotency_key="photos-upload",
            tags=["photos"],
        )


def test_file_preflight_returns_random_ingress_secret_and_persists_only_envelope(
    tmp_path: Path,
) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    _seed_tag(path, "encrypted")
    upload_store = UploadStoreStub()
    service = _service(path, upload_store)
    created = service.create_or_resume_upload_session(
        idempotency_key="encrypted-upload",
        tags=["encrypted"],
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
    _seed_tag(path, "repeat")
    service = _service(path)
    upload_ids: list[str] = []

    for index in range(2):
        created = service.create_or_resume_upload_session(
            idempotency_key=f"repeat-upload-{index}",
            tags=["repeat"],
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
    _seed_tag(path, "cancel-me")
    service = _service(path, upload_store)
    created = service.create_or_resume_upload_session(
        idempotency_key="cancel-upload",
        tags=["cancel-me"],
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


def test_batch_collection_upload_can_be_canceled_before_all_bytes_arrive(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    upload_store = MissingTargetUploadStore()
    _seed_tag(path, "cancel-batch")
    service = _service(path, upload_store)
    created = service.create_or_resume_upload(
        idempotency_key="cancel-batch-upload",
        tags=["cancel-batch"],
        files=[{"path": "one.txt", "bytes": 10, "sha256": "a" * 64}],
    )
    collection_id = str(created["collection_id"])
    service.create_or_resume_file_upload(collection_id, "one.txt")

    canceled = service.cancel_upload_session(collection_id)

    assert canceled["state"] == "canceled"
    assert len(upload_store.canceled) == 1
    assert len(upload_store.deleted) == 1
    with pytest.raises(NotFound, match="collection upload not found"):
        service.get_upload(collection_id)


def test_collection_upload_completion_verifies_targets_with_bounded_concurrency(
    tmp_path: Path,
) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    file_count = 8
    worker_count = 4
    upload_store = ConcurrentVerifyUploadStore(worker_count)
    _seed_tag(path, "verify-me")
    service = _service(path, upload_store)
    created = service.create_or_resume_upload_session(
        idempotency_key="verify-upload",
        tags=["verify-me"],
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
    _seed_tag(path, "missing-target")
    service = _service(path, upload_store)
    created = service.create_or_resume_upload_session(
        idempotency_key="missing-target-upload",
        tags=["missing-target"],
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

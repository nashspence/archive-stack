from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from riverhog_api_client.ingress import iter_ingress_upload_parts
from riverhog_core.archive_objects import iter_verified_file_chunks
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CatalogEventRecord,
    CollectionArchiveCopyRecord,
    CollectionArchiveObjectRecord,
    CollectionFileRecord,
    CollectionUploadFileRecord,
    CollectionUploadRecord,
    IngressCleanupRecord,
    RetrievalCacheLeaseRecord,
    RetrievalCacheObjectRecord,
)
from riverhog_core.ingress_crypto import (
    create_ingress_encryption,
    ingress_encryption_descriptor,
)
from riverhog_core.portable_catalog import portable_collection_manifest
from riverhog_core.ports.archive_store import CollectionArchiveUploadReceipt
from riverhog_core.ports.retrieval_cache import RetrievalCacheReceipt
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_uploads import SqlAlchemyArchiveUploadService
from riverhog_core.services.collections import _collection_upload_target_path
from riverhog_core.tusd_ids import tusd_upload_id_for_target_path
from time_formats import format_utc_timestamp, parse_utc_timestamp, utc_now

from tests.fixtures.crypto import FixtureProofStamper
from tests.unit.archive_object_fixtures import (
    COLLECTION_ID,
    MemoryArchiveStore,
    as_archive_store,
)
from tests.unit.db_helpers import sqlite_url

CONTENT = b"archive upload policy\n"


class MemoryUploadStore:
    def __init__(self) -> None:
        self.targets: dict[str, bytes] = {}

    def iter_target(
        self,
        target_path: str,
        *,
        offset: int = 0,
        size: int | None = None,
    ) -> Iterator[bytes]:
        content = self.targets[target_path]
        yield content[offset:] if size is None else content[offset : offset + size]

    def delete_target(self, target_path: str) -> None:
        self.targets.pop(target_path, None)


class CachedMemoryArchiveStore(MemoryArchiveStore):
    def upload_collection_archive(self, **kwargs: object) -> CollectionArchiveUploadReceipt:
        receipt = super().upload_collection_archive(**kwargs)  # type: ignore[arg-type]
        return CollectionArchiveUploadReceipt(
            objects=tuple(
                replace(
                    current,
                    ingestion_cache=RetrievalCacheReceipt(
                        object_path=f"cache/{current.object_id}",
                        version_id=f"version-{current.object_id}",
                        stored_bytes=current.stored_bytes,
                        stored_sha256="f" * 64,
                        cached_at="2026-07-18T00:00:00.000000Z",
                        verified_at="2026-07-18T00:00:00.000000Z",
                    ),
                )
                if current.kind in {"pack", "file", "segment"}
                else current
                for current in receipt.objects
            )
        )


def _stage(
    path: Path,
    upload_store: MemoryUploadStore,
    *,
    collection_id: str = COLLECTION_ID,
    file_path: str = "document.txt",
    content: bytes = CONTENT,
) -> RuntimeConfig:
    database_url = sqlite_url(path / "catalog.sqlite3")
    initialize_db(database_url)
    config = RuntimeConfig(database_url=database_url)
    encryption = create_ingress_encryption(
        config,
        collection_id=collection_id,
        path=file_path,
        plaintext_bytes=len(content),
    )
    descriptor = ingress_encryption_descriptor(
        config,
        collection_id=collection_id,
        path=file_path,
        plaintext_bytes=len(content),
        ciphertext_bytes=encryption.ciphertext_bytes,
        secret_envelope=encryption.secret_envelope,
        state_json=encryption.state_json,
    )
    source = path / file_path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(content)
    file_record = CollectionUploadFileRecord(
        collection_id=collection_id,
        path=file_path,
        file_order=1,
        bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        ingress_bytes=encryption.ciphertext_bytes,
        ingress_uploaded_bytes=encryption.ciphertext_bytes,
        ingress_secret_envelope=encryption.secret_envelope,
        ingress_state_json=encryption.state_json,
        ingress_upload_id="",
    )
    target = _collection_upload_target_path(file_record)
    file_record.ingress_upload_id = tusd_upload_id_for_target_path(target)
    upload_store.targets[target] = b"".join(
        part.ciphertext
        for part in iter_ingress_upload_parts(
            source,
            descriptor,
            ciphertext_offset=0,
            target_part_bytes=1024 * 1024,
        )
    )
    with session_scope(make_session_factory(database_url)) as session:
        session.add(
            CollectionUploadRecord(
                collection_id=collection_id,
                archive_store="deep",
                state="archiving",
            )
        )
        session.add(file_record)
    return config


def test_encrypted_ingress_streams_into_independently_restorable_archive_objects(
    tmp_path: Path,
) -> None:
    upload_store = MemoryUploadStore()
    archive_store = MemoryArchiveStore()
    config = _stage(tmp_path, upload_store)
    service = SqlAlchemyArchiveUploadService(
        config,
        ArchiveStoreRegistry({"deep": as_archive_store(archive_store)}),
        upload_store=upload_store,  # type: ignore[arg-type]
        proof_stamper=FixtureProofStamper(),
    )

    assert service.process_due_uploads(limit=1) == 1
    assert upload_store.targets
    assert service.ingress_cleanup_status()["pending"] == 1
    assert service.process_due_ingress_cleanup() == 1

    with session_scope(make_session_factory(config.database_url)) as session:
        copy = session.get(CollectionArchiveCopyRecord, (COLLECTION_ID, "deep"))
        assert copy is not None and copy.state == "uploaded"
        objects = session.query(CollectionArchiveObjectRecord).order_by(
            CollectionArchiveObjectRecord.object_order
        )
        assert [(row.kind, row.object_id) for row in objects] == [
            ("pack", "data-000000"),
            ("manifest", "manifest"),
            ("proof", "proof"),
        ]
        file = session.get(CollectionFileRecord, (COLLECTION_ID, "document.txt"))
        assert file is not None and file.sha256 == hashlib.sha256(CONTENT).hexdigest()
        assert session.get(CollectionUploadRecord, COLLECTION_ID) is None
        event = session.query(CatalogEventRecord).one()
        assert event.change == "created" and event.collection_id == COLLECTION_ID
        _manifest, expected_etag = portable_collection_manifest(
            COLLECTION_ID,
            (("document.txt", len(CONTENT), hashlib.sha256(CONTENT).hexdigest()),),
        )
        assert event.manifest_etag == expected_etag
    assert archive_store.archive is not None
    chunks, _size = iter_verified_file_chunks(
        archive_store.archive,
        path="document.txt",
        read_object=lambda object_id: archive_store.archive.require_object(
            object_id
        ).iter_plaintext(),
    )
    assert b"".join(chunks) == CONTENT
    assert upload_store.targets == {}
    assert len(archive_store.catalog_entries) == 1


def test_restore_required_ingest_records_the_initial_cache_lease(tmp_path: Path) -> None:
    upload_store = MemoryUploadStore()
    archive_store = CachedMemoryArchiveStore(read_mode="restore_required")
    config = replace(
        _stage(tmp_path, upload_store),
        retrieval_initial_ingestion_lease=timedelta(days=30),
    )
    service = SqlAlchemyArchiveUploadService(
        config,
        ArchiveStoreRegistry({"deep": as_archive_store(archive_store)}),
        upload_store=upload_store,  # type: ignore[arg-type]
        proof_stamper=FixtureProofStamper(),
    )

    assert service.process_due_uploads(limit=1) == 1
    assert service.process_due_ingress_cleanup() == 1

    with session_scope(make_session_factory(config.database_url)) as session:
        cached = session.query(RetrievalCacheObjectRecord).one()
        lease = session.query(RetrievalCacheLeaseRecord).one()
        assert (cached.source_store, cached.collection_id, cached.object_id) == (
            "deep",
            COLLECTION_ID,
            "data-000000",
        )
        assert lease.owner == "initial-ingestion"
        remaining = parse_utc_timestamp(lease.expires_at) - utc_now()
        assert timedelta(days=29, hours=23) < remaining <= timedelta(days=30)


class BlockingDeleteUploadStore(MemoryUploadStore):
    def __init__(self, *, expected_concurrency: int = 1) -> None:
        super().__init__()
        self.expected_concurrency = expected_concurrency
        self.delete_started = threading.Event()
        self.release_delete = threading.Event()
        self._lock = threading.Lock()
        self.active_deletes = 0
        self.max_active_deletes = 0

    def delete_target(self, target_path: str) -> None:
        with self._lock:
            self.active_deletes += 1
            self.max_active_deletes = max(self.max_active_deletes, self.active_deletes)
            if self.active_deletes >= self.expected_concurrency:
                self.delete_started.set()
        assert self.release_delete.wait(timeout=5)
        try:
            super().delete_target(target_path)
        finally:
            with self._lock:
                self.active_deletes -= 1


def test_ingress_cleanup_does_not_block_the_next_archive(tmp_path: Path) -> None:
    upload_store = BlockingDeleteUploadStore()
    archive_store = MemoryArchiveStore()
    config = _stage(tmp_path, upload_store)
    service = SqlAlchemyArchiveUploadService(
        config,
        ArchiveStoreRegistry({"deep": as_archive_store(archive_store)}),
        upload_store=upload_store,  # type: ignore[arg-type]
        proof_stamper=FixtureProofStamper(),
    )
    assert service.process_due_uploads(limit=1) == 1

    with ThreadPoolExecutor(max_workers=1) as executor:
        cleanup = executor.submit(service.process_due_ingress_cleanup)
        assert upload_store.delete_started.wait(timeout=5)
        _stage(
            tmp_path,
            upload_store,
            collection_id="next/20260102T030406Z",
            file_path="next.txt",
            content=b"next archive\n",
        )
        assert service.process_due_uploads(limit=1) == 1
        upload_store.release_delete.set()
        assert cleanup.result(timeout=5) == 1

    with session_scope(make_session_factory(config.database_url)) as session:
        assert session.get(CollectionFileRecord, ("next/20260102T030406Z", "next.txt"))


def test_ingress_cleanup_is_bounded_and_restart_safe(tmp_path: Path) -> None:
    upload_store = BlockingDeleteUploadStore(expected_concurrency=4)
    database_url = sqlite_url(tmp_path / "catalog.sqlite3")
    initialize_db(database_url)
    config = RuntimeConfig(
        database_url=database_url,
        ingress_cleanup_concurrency=4,
    )
    created_at = "2026-07-24T00:00:00.000000Z"
    with session_scope(make_session_factory(database_url)) as session:
        for index in range(12):
            target_path = f"cleanup/{index}"
            upload_store.targets[target_path] = b"ciphertext"
            session.add(
                IngressCleanupRecord(
                    target_path=target_path,
                    collection_id=f"cleanup/{index}",
                    ingress_upload_id=f"upload-{index}",
                    state="deleting" if index == 0 else "pending",
                    attempt_count=1 if index == 0 else 0,
                    created_at=created_at,
                    next_attempt_at=created_at,
                    last_attempt_at=created_at if index == 0 else None,
                )
            )

    restarted_service = SqlAlchemyArchiveUploadService(
        config,
        ArchiveStoreRegistry({"deep": as_archive_store(MemoryArchiveStore())}),
        upload_store=upload_store,  # type: ignore[arg-type]
        proof_stamper=FixtureProofStamper(),
    )
    assert restarted_service.requeue_interrupted_ingress_cleanup_for_startup() == 1
    with ThreadPoolExecutor(max_workers=1) as executor:
        cleanup = executor.submit(restarted_service.process_due_ingress_cleanup)
        assert upload_store.delete_started.wait(timeout=5)
        assert upload_store.max_active_deletes == 4
        assert restarted_service.ingress_cleanup_status() == {
            "total": 12,
            "pending": 0,
            "deleting": 12,
            "failed": 0,
            "oldest_created_at": created_at,
        }
        upload_store.release_delete.set()
        assert cleanup.result(timeout=5) == 12
    assert restarted_service.ingress_cleanup_status()["total"] == 0
    assert upload_store.targets == {}


class FlakyDeleteUploadStore(MemoryUploadStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail = True
        self.delete_calls = 0

    def delete_target(self, target_path: str) -> None:
        self.delete_calls += 1
        if self.fail:
            raise OSError("temporary cleanup failure")
        super().delete_target(target_path)


def test_ingress_cleanup_failure_is_visible_and_retried(tmp_path: Path) -> None:
    upload_store = FlakyDeleteUploadStore()
    archive_store = MemoryArchiveStore()
    config = _stage(tmp_path, upload_store)
    service = SqlAlchemyArchiveUploadService(
        config,
        ArchiveStoreRegistry({"deep": as_archive_store(archive_store)}),
        upload_store=upload_store,  # type: ignore[arg-type]
        proof_stamper=FixtureProofStamper(),
    )
    assert service.process_due_uploads(limit=1) == 1
    assert service.process_due_ingress_cleanup() == 1
    status = service.ingress_cleanup_status()
    assert status["failed"] == 1

    with session_scope(make_session_factory(config.database_url)) as session:
        record = session.query(IngressCleanupRecord).one()
        assert record.last_error == "temporary cleanup failure"
        record.next_attempt_at = format_utc_timestamp(utc_now())
    upload_store.fail = False
    assert service.process_due_ingress_cleanup() == 1
    assert service.ingress_cleanup_status()["total"] == 0
    assert upload_store.delete_calls == 2


def test_ingress_cleanup_preserves_a_target_owned_by_an_active_upload(tmp_path: Path) -> None:
    upload_store = FlakyDeleteUploadStore()
    upload_store.fail = False
    config = _stage(tmp_path, upload_store)
    with session_scope(make_session_factory(config.database_url)) as session:
        file_record = session.query(CollectionUploadFileRecord).one()
        target_path = _collection_upload_target_path(file_record)
        current_text = format_utc_timestamp(utc_now())
        session.add(
            IngressCleanupRecord(
                target_path=target_path,
                collection_id=COLLECTION_ID,
                ingress_upload_id=file_record.ingress_upload_id,
                state="pending",
                attempt_count=0,
                created_at=current_text,
                next_attempt_at=current_text,
            )
        )
    service = SqlAlchemyArchiveUploadService(
        config,
        ArchiveStoreRegistry({"deep": as_archive_store(MemoryArchiveStore())}),
        upload_store=upload_store,  # type: ignore[arg-type]
        proof_stamper=FixtureProofStamper(),
    )

    assert service.process_due_ingress_cleanup() == 1
    assert service.ingress_cleanup_status()["failed"] == 1
    assert upload_store.delete_calls == 0
    assert upload_store.targets

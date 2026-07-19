from __future__ import annotations

import hashlib
from collections.abc import Iterator
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
from time_formats import parse_utc_timestamp, utc_now

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


def _stage(path: Path, upload_store: MemoryUploadStore) -> RuntimeConfig:
    database_url = sqlite_url(path / "catalog.sqlite3")
    initialize_db(database_url)
    config = RuntimeConfig(database_url=database_url)
    target = _collection_upload_target_path(COLLECTION_ID, "document.txt")
    encryption = create_ingress_encryption(
        config,
        collection_id=COLLECTION_ID,
        path="document.txt",
        plaintext_bytes=len(CONTENT),
    )
    descriptor = ingress_encryption_descriptor(
        config,
        collection_id=COLLECTION_ID,
        path="document.txt",
        plaintext_bytes=len(CONTENT),
        ciphertext_bytes=encryption.ciphertext_bytes,
        secret_envelope=encryption.secret_envelope,
        state_json=encryption.state_json,
    )
    source = path / "document.txt"
    source.write_bytes(CONTENT)
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
                collection_id=COLLECTION_ID,
                archive_store="deep",
                state="archiving",
            )
        )
        session.add(
            CollectionUploadFileRecord(
                collection_id=COLLECTION_ID,
                path="document.txt",
                file_order=1,
                bytes=len(CONTENT),
                sha256=hashlib.sha256(CONTENT).hexdigest(),
                ingress_bytes=encryption.ciphertext_bytes,
                ingress_uploaded_bytes=encryption.ciphertext_bytes,
                ingress_secret_envelope=encryption.secret_envelope,
                ingress_state_json=encryption.state_json,
                ingress_upload_id=tusd_upload_id_for_target_path(target),
            )
        )
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

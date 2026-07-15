from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveCopyRecord,
    CollectionArchiveObjectRecord,
    CollectionFileRecord,
    CollectionUploadFileRecord,
    CollectionUploadRecord,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_uploads import SqlAlchemyArchiveUploadService
from riverhog_core.services.collections import _collection_upload_target_path
from tests.fixtures.crypto import FixtureProofStamper
from tests.unit.archive_object_fixtures import (
    COLLECTION_ID,
    MemoryArchiveStore,
    MemoryHotStore,
    as_archive_store,
    as_hot_store,
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


def _stage(path: Path, upload_store: MemoryUploadStore, *, retain_hot: bool) -> RuntimeConfig:
    database_url = sqlite_url(path)
    initialize_db(database_url)
    target = _collection_upload_target_path(COLLECTION_ID, "document.txt")
    upload_store.targets[target] = CONTENT
    with session_scope(make_session_factory(database_url)) as session:
        session.add(
            CollectionUploadRecord(
                collection_id=COLLECTION_ID,
                archive_store="deep",
                state="archiving",
                retain_hot=retain_hot,
            )
        )
        session.add(
            CollectionUploadFileRecord(
                collection_id=COLLECTION_ID,
                path="document.txt",
                file_order=1,
                bytes=len(CONTENT),
                sha256=hashlib.sha256(CONTENT).hexdigest(),
                uploaded_bytes=len(CONTENT),
            )
        )
    return RuntimeConfig(database_url=database_url)


def _process(
    config: RuntimeConfig,
    upload_store: MemoryUploadStore,
    archive_store: MemoryArchiveStore,
    hot_store: MemoryHotStore | None,
) -> None:
    service = SqlAlchemyArchiveUploadService(
        config,
        ArchiveStoreRegistry({"deep": as_archive_store(archive_store)}, default_store="deep"),
        as_hot_store(hot_store) if hot_store is not None else None,
        upload_store=upload_store,  # type: ignore[arg-type]
        proof_stamper=FixtureProofStamper(),
    )
    assert service.process_due_uploads(limit=1) == 1


def test_upload_records_independently_restorable_objects(tmp_path: Path) -> None:
    upload_store = MemoryUploadStore()
    archive_store = MemoryArchiveStore()
    config = _stage(tmp_path / "catalog.sqlite3", upload_store, retain_hot=False)

    _process(config, upload_store, archive_store, None)

    with session_scope(make_session_factory(config.database_url)) as session:
        copy = session.get(CollectionArchiveCopyRecord, (COLLECTION_ID, "deep"))
        assert copy is not None
        assert copy.state == "uploaded"
        objects = session.query(CollectionArchiveObjectRecord).order_by(
            CollectionArchiveObjectRecord.object_order
        )
        assert [(row.kind, row.object_id) for row in objects] == [
            ("pack", "data-000000"),
            ("manifest", "manifest"),
            ("proof", "proof"),
        ]
        file = session.get(CollectionFileRecord, (COLLECTION_ID, "document.txt"))
        assert file is not None and file.hot is False
        assert session.get(CollectionUploadRecord, COLLECTION_ID) is None
    assert archive_store.archive is not None
    assert upload_store.targets == {}


def test_upload_retains_hot_materialization_by_default_policy(tmp_path: Path) -> None:
    upload_store = MemoryUploadStore()
    archive_store = MemoryArchiveStore()
    hot_store = MemoryHotStore()
    config = _stage(tmp_path / "catalog.sqlite3", upload_store, retain_hot=True)

    _process(config, upload_store, archive_store, hot_store)

    assert hot_store.files[(COLLECTION_ID, "document.txt")] == CONTENT
    with session_scope(make_session_factory(config.database_url)) as session:
        file = session.get(CollectionFileRecord, (COLLECTION_ID, "document.txt"))
        assert file is not None and file.hot is True


def test_upload_publishes_object_manifest_to_restore_catalog(tmp_path: Path) -> None:
    upload_store = MemoryUploadStore()
    archive_store = MemoryArchiveStore()
    config = _stage(tmp_path / "catalog.sqlite3", upload_store, retain_hot=False)

    _process(config, upload_store, archive_store, None)

    assert len(archive_store.catalog_entries) == 1
    entry = archive_store.catalog_entries[0]
    assert entry["collection_id"] == COLLECTION_ID
    assert [current["kind"] for current in entry["objects"]] == [
        "pack",
        "manifest",
        "proof",
    ]

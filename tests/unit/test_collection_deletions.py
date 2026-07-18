from __future__ import annotations

from pathlib import Path
from typing import cast

from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import CatalogEventRecord, CollectionRecord
from riverhog_core.ports.upload_store import UploadStore
from riverhog_core.services.collection_deletions import SqlAlchemyCollectionDeletionService
from tests.unit.archive_object_fixtures import (
    COLLECTION_ID,
    MemoryArchiveStore,
    as_archive_store,
    seed_archive_copy,
)

FILES = {"one.txt": b"first file\n", "two.txt": b"second file\n"}


class NoopUploadStore:
    def cancel_upload(self, upload_url: str) -> None:
        raise AssertionError(upload_url)

    def delete_target(self, target_path: str) -> None:
        raise AssertionError(target_path)


def _service(path: Path):
    config, archive = seed_archive_copy(path, FILES)
    archive_store = MemoryArchiveStore(archive)
    service = SqlAlchemyCollectionDeletionService(
        config,
        ArchiveStoreRegistry({"deep": as_archive_store(archive_store)}),
        cast(UploadStore, NoopUploadStore()),
        None,
    )
    return config, archive_store, service


def test_deletion_plan_uses_catalog_object_and_file_aggregates(tmp_path: Path) -> None:
    _config, _archive_store, service = _service(tmp_path / "catalog.sqlite3")

    plan = service.plan(COLLECTION_ID)

    assert plan["status"] == "ready"
    assert plan["file_count"] == 2
    assert plan["bytes"] == sum(map(len, FILES.values()))
    assert [current["kind"] for current in plan["archive_objects"]] == [
        "pack",
        "manifest",
        "proof",
    ]
    assert plan["remote_storage_bytes"] == sum(
        int(current["stored_bytes"]) for current in plan["archive_objects"]
    )


def test_confirmed_deletion_removes_archive_and_publishes_catalog_change(
    tmp_path: Path,
) -> None:
    config, archive_store, service = _service(tmp_path / "catalog.sqlite3")
    challenge = str(service.plan(COLLECTION_ID)["challenge"])

    result = service.delete(COLLECTION_ID, challenge=challenge)

    assert result["status"] == "deleted"
    assert archive_store.deleted == [("data-000000", "manifest", "proof")]
    assert archive_store.catalog_entries == []
    with session_scope(make_session_factory(config.database_url)) as session:
        assert session.get(CollectionRecord, COLLECTION_ID) is None
        event = session.query(CatalogEventRecord).one()
        assert event.change == "deleted" and event.collection_id == COLLECTION_ID

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ArchiveRestoreCollectionRecord,
    ArchiveRestoreRecord,
    CollectionArchiveRecord,
    CollectionDeletionRecord,
    CollectionFileRecord,
    CollectionRecord,
    CollectionUploadFileRecord,
    CollectionUploadRecord,
    FetchRecord,
    FetchSelectorRecord,
)
from riverhog_core.domain.errors import Conflict
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.collection_deletions import SqlAlchemyCollectionDeletionService
from tests.unit.db_helpers import sqlite_url


class FakeHotStore:
    def __init__(self) -> None:
        self.files = {("docs", "readme.txt"): b"sole durable copy\n"}

    def list_collection_files(self, collection_id: str) -> list[tuple[str, int]]:
        return [
            (path, len(content))
            for (current_collection, path), content in sorted(self.files.items())
            if current_collection == collection_id
        ]

    def delete_collection_file(self, collection_id: str, path: str) -> None:
        self.files.pop((collection_id, path), None)


class FakeArchiveStore:
    def __init__(self) -> None:
        self.objects = {
            "archive/archives/opaque-docs/archive.tar.age",
            "archive/archives/opaque-docs/manifest.yml.age",
            "archive/archives/opaque-docs/manifest.yml.ots.age",
        }
        self.catalog_entries: list[dict[str, object]] | None = None
        self.fail_publish_once = False

    def delete_collection_archive_package(
        self,
        *,
        collection_id: str,
        object_path: str,
        manifest_object_path: str,
        proof_object_path: str,
    ) -> None:
        assert collection_id == "docs"
        for path in (object_path, manifest_object_path, proof_object_path):
            self.objects.discard(path)

    def publish_restore_catalog(
        self,
        *,
        entries: list[dict[str, object]],
        generated_at: str,
    ) -> None:
        assert generated_at.endswith("Z")
        if self.fail_publish_once:
            self.fail_publish_once = False
            raise RuntimeError("synthetic catalog publication failure")
        self.catalog_entries = entries


class FakeUploadStore:
    def __init__(self) -> None:
        self.canceled: list[str] = []
        self.deleted: list[str] = []

    def cancel_upload(self, tus_url: str) -> None:
        self.canceled.append(tus_url)

    def delete_target(self, target_path: str) -> None:
        self.deleted.append(target_path)


def _seed(path: Path, *, active_restore: bool = False, active_fetch: bool = False) -> None:
    content = b"sole durable copy\n"
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        session.add(CollectionRecord(id="docs"))
        session.add(
            CollectionFileRecord(
                collection_id="docs",
                path="readme.txt",
                bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                hot=True,
            )
        )
        session.add(
            CollectionArchiveRecord(
                collection_id="docs",
                state="uploaded",
                archive_storage_prefix="archive/archives/opaque-docs",
                object_path="archive/archives/opaque-docs/archive.tar.age",
                stored_bytes=100,
                sha256="a" * 64,
                manifest_object_path="archive/archives/opaque-docs/manifest.yml.age",
                manifest_sha256="b" * 64,
                manifest_stored_bytes=20,
                ots_object_path="archive/archives/opaque-docs/manifest.yml.ots.age",
                ots_sha256="c" * 64,
                ots_stored_bytes=10,
                last_verified_at="2026-07-14T00:00:00Z",
            )
        )
        session.add(
            CollectionUploadRecord(
                collection_id="docs",
                state="expired",
            )
        )
        session.add(
            CollectionUploadFileRecord(
                collection_id="docs",
                path="readme.txt",
                file_order=0,
                bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                uploaded_bytes=0,
                tus_url="http://tusd.invalid/files/docs",
            )
        )
        restore_id = "ar-active" if active_restore else "ar-complete"
        session.add(
            ArchiveRestoreRecord(
                restore_id=restore_id,
                state="requested" if active_restore else "completed",
                created_at="2026-07-14T00:00:00Z",
                retrieval_tier="bulk",
                hold_days=7,
                warnings_json="[]",
            )
        )
        session.add(
            ArchiveRestoreCollectionRecord(
                restore_id=restore_id,
                collection_id="docs",
                collection_order=0,
            )
        )
        if active_fetch:
            session.add(
                FetchRecord(
                    fetch_id="fx-1",
                    name="active docs",
                    fetch_order=1,
                    fetch_state="queued_archive",
                )
            )
            session.add(
                FetchSelectorRecord(
                    fetch_id="fx-1",
                    target="docs/",
                    selector_order=0,
                )
            )


def _service(
    path: Path,
    archive_store: FakeArchiveStore,
    hot_store: FakeHotStore,
    upload_store: FakeUploadStore,
) -> SqlAlchemyCollectionDeletionService:
    return SqlAlchemyCollectionDeletionService(
        RuntimeConfig(database_url=sqlite_url(path)),
        archive_store,
        hot_store,
        upload_store,
    )


def _setup(
    tmp_path: Path,
    *,
    active_restore: bool = False,
    active_fetch: bool = False,
) -> tuple[
    Path,
    SqlAlchemyCollectionDeletionService,
    FakeArchiveStore,
    FakeHotStore,
    FakeUploadStore,
]:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    _seed(path, active_restore=active_restore, active_fetch=active_fetch)
    archive_store = FakeArchiveStore()
    hot_store = FakeHotStore()
    upload_store = FakeUploadStore()
    return (
        path,
        _service(path, archive_store, hot_store, upload_store),
        archive_store,
        hot_store,
        upload_store,
    )


def test_plan_enumerates_custody_impact_and_issues_state_bound_challenge(tmp_path: Path) -> None:
    path, service, _, hot_store, _ = _setup(tmp_path)

    plan = service.plan("docs")

    assert plan["status"] == "ready"
    assert "sole durable copies" in str(plan["warning"])
    assert str(plan["challenge"]).startswith("delete-")
    assert plan["file_count"] == 1
    assert plan["bytes"] == len(b"sole durable copy\n")
    assert plan["remote_storage_bytes"] == 130
    assert plan["metadata_rows"] == {
        "collections": 1,
        "collection_files": 1,
        "collection_archives": 1,
        "collection_uploads": 1,
        "collection_upload_files": 1,
        "archive_restores": 1,
        "archive_restore_collections": 1,
        "encrypted_restore_catalog_entries": 1,
    }
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        assert session.get(CollectionDeletionRecord, "docs") is None

    hot_store.files[("docs", "second.txt")] = b"changed"
    with pytest.raises(Conflict, match="plan changed"):
        service.delete("docs", challenge=str(plan["challenge"]))


def test_plan_reports_active_restore_and_fetch_blockers(tmp_path: Path) -> None:
    _, service, _, _, _ = _setup(tmp_path, active_restore=True, active_fetch=True)

    plan = service.plan("docs")

    assert plan["status"] == "blocked"
    assert plan["challenge"] is None
    assert plan["blockers"] == [
        "archive restore is active: ar-active",
        "fetch is active: fx-1",
    ]


def test_delete_removes_collection_objects_and_dependent_state(tmp_path: Path) -> None:
    path, service, archive_store, hot_store, upload_store = _setup(tmp_path)
    plan = service.plan("docs")

    result = service.delete("docs", challenge=str(plan["challenge"]))

    assert result == {
        "status": "deleted",
        "collection_id": "docs",
        "files": 1,
        "bytes": len(b"sole durable copy\n"),
        "remote_storage_bytes": 130,
    }
    assert archive_store.objects == set()
    assert archive_store.catalog_entries == []
    assert hot_store.files == {}
    assert upload_store.canceled == ["http://tusd.invalid/files/docs"]
    assert upload_store.deleted == ["/.riverhog/uploads/collections/docs/readme.txt"]
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        assert session.get(CollectionRecord, "docs") is None
        assert session.get(CollectionUploadRecord, "docs") is None
        assert session.get(ArchiveRestoreRecord, "ar-complete") is None
        assert session.get(CollectionDeletionRecord, "docs") is None


def test_delete_retries_after_catalog_publication_failure(tmp_path: Path) -> None:
    path, service, archive_store, _, _ = _setup(tmp_path)
    archive_store.fail_publish_once = True
    plan = service.plan("docs")
    challenge = str(plan["challenge"])

    with pytest.raises(RuntimeError, match="synthetic catalog"):
        service.delete("docs", challenge=challenge)

    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        assert session.get(CollectionRecord, "docs") is not None
        assert session.get(CollectionDeletionRecord, "docs") is not None
    assert archive_store.objects == set()
    assert service.plan("docs")["challenge"] == challenge

    result = service.delete("docs", challenge=challenge)

    assert result["status"] == "deleted"
    with session_scope(factory) as session:
        assert session.get(CollectionRecord, "docs") is None
        assert session.get(CollectionDeletionRecord, "docs") is None

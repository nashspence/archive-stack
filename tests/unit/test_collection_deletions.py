from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ArchiveCopyJobRecord,
    ArchiveRestoreCollectionRecord,
    ArchiveRestoreRecord,
    CollectionArchiveCopyRecord,
    CollectionDeletionRecord,
    CollectionFileRecord,
    CollectionRecord,
    CollectionUploadFileRecord,
    CollectionUploadRecord,
    FetchCollectionRecord,
    FetchRecord,
)
from riverhog_core.domain.errors import Conflict
from riverhog_core.ports.hot_store import HotCollectionFile, HotCollectionListing
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.collection_deletions import SqlAlchemyCollectionDeletionService
from tests.unit.db_helpers import sqlite_url

FailurePoint = Literal[
    "hot_object",
    "upload_resource",
    "upload_target",
    "archive_package",
    "recovery_catalog",
]


class SyntheticBoundaryFailure(RuntimeError):
    pass


class FailOnce:
    def __init__(self, point: FailurePoint | None = None) -> None:
        self.point = point

    def after(self, point: FailurePoint) -> None:
        if self.point != point:
            return
        self.point = None
        raise SyntheticBoundaryFailure(f"synthetic deletion boundary failure: {point}")


class FakeHotStore:
    def __init__(self, failures: FailOnce, *, include_unrelated: bool = False) -> None:
        self._failures = failures
        self.files = {("2025/20250102T030405Z__docs", "readme.txt"): b"sole durable copy\n"}
        if include_unrelated:
            self.files[("2025/20250103T030405Z__other", "keep.txt")] = b"unrelated durable copy\n"

    def list_collection_files(self, collection_id: str) -> HotCollectionListing:
        files = tuple(
            HotCollectionFile(path=path, bytes=len(content))
            for (current_collection, path), content in sorted(self.files.items())
            if current_collection == collection_id
        )
        return HotCollectionListing(
            files=files,
            file_count=len(files),
            total_bytes=sum(file.bytes for file in files),
        )

    def delete_collection_file(self, collection_id: str, path: str) -> None:
        self.files.pop((collection_id, path), None)
        self._failures.after("hot_object")


class FakeArchiveStore:
    def __init__(self, failures: FailOnce, *, include_unrelated: bool = False) -> None:
        self._failures = failures
        self.objects = {
            "archive/archives/opaque-docs/archive.tar.age",
            "archive/archives/opaque-docs/manifest.yml.age",
            "archive/archives/opaque-docs/manifest.yml.ots.age",
        }
        if include_unrelated:
            self.objects.update(
                {
                    "archive/archives/opaque-other/archive.tar.age",
                    "archive/archives/opaque-other/manifest.yml.age",
                    "archive/archives/opaque-other/manifest.yml.ots.age",
                }
            )
        self.catalog_entries: list[dict[str, object]] | None = None

    def delete_collection_archive_package(
        self,
        *,
        collection_id: str,
        object_path: str,
        manifest_object_path: str,
        proof_object_path: str,
    ) -> None:
        assert collection_id == "2025/20250102T030405Z__docs"
        for index, path in enumerate((object_path, manifest_object_path, proof_object_path)):
            self.objects.discard(path)
            if index == 0:
                self._failures.after("archive_package")

    def publish_restore_catalog(
        self,
        *,
        entries: list[dict[str, object]],
        generated_at: str,
    ) -> None:
        assert generated_at.endswith("Z")
        self.catalog_entries = entries
        self._failures.after("recovery_catalog")


class FakeUploadStore:
    def __init__(self, failures: FailOnce) -> None:
        self._failures = failures
        self.canceled: list[str] = []
        self.deleted: list[str] = []

    def cancel_upload(self, tus_url: str) -> None:
        self.canceled.append(tus_url)
        self._failures.after("upload_resource")

    def delete_target(self, target_path: str) -> None:
        self.deleted.append(target_path)
        self._failures.after("upload_target")


def _seed(
    path: Path,
    *,
    active_restore: bool = False,
    active_fetch: bool = False,
    include_unrelated: bool = False,
) -> None:
    content = b"sole durable copy\n"
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        session.add(CollectionRecord(id="2025/20250102T030405Z__docs"))
        session.add(
            CollectionFileRecord(
                collection_id="2025/20250102T030405Z__docs",
                path="readme.txt",
                bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                hot=True,
            )
        )
        session.add(
            CollectionArchiveCopyRecord(
                collection_id="2025/20250102T030405Z__docs",
                store="deep",
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
                collection_id="2025/20250102T030405Z__docs",
                archive_store="deep",
                state="expired",
            )
        )
        session.add(
            CollectionUploadFileRecord(
                collection_id="2025/20250102T030405Z__docs",
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
                collection_id="2025/20250102T030405Z__docs",
                archive_store="deep",
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
                FetchCollectionRecord(
                    fetch_id="fx-1",
                    collection_id="2025/20250102T030405Z__docs",
                    collection_order=0,
                )
            )
        if include_unrelated:
            other_content = b"unrelated durable copy\n"
            session.add(CollectionRecord(id="2025/20250103T030405Z__other"))
            session.add(
                CollectionFileRecord(
                    collection_id="2025/20250103T030405Z__other",
                    path="keep.txt",
                    bytes=len(other_content),
                    sha256=hashlib.sha256(other_content).hexdigest(),
                    hot=True,
                )
            )
            session.add(
                CollectionArchiveCopyRecord(
                    collection_id="2025/20250103T030405Z__other",
                    store="deep",
                    state="uploaded",
                    archive_storage_prefix="archive/archives/opaque-other",
                    object_path="archive/archives/opaque-other/archive.tar.age",
                    stored_bytes=200,
                    sha256="d" * 64,
                    manifest_object_path="archive/archives/opaque-other/manifest.yml.age",
                    manifest_sha256="e" * 64,
                    manifest_stored_bytes=30,
                    ots_object_path="archive/archives/opaque-other/manifest.yml.ots.age",
                    ots_sha256="f" * 64,
                    ots_stored_bytes=10,
                    last_verified_at="2026-07-14T00:00:00Z",
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
        ArchiveStoreRegistry({"deep": archive_store}, default_store="deep"),
        hot_store,
        upload_store,
    )


def _setup(
    tmp_path: Path,
    *,
    active_restore: bool = False,
    active_fetch: bool = False,
    include_unrelated: bool = False,
    failure_point: FailurePoint | None = None,
) -> tuple[
    Path,
    SqlAlchemyCollectionDeletionService,
    FakeArchiveStore,
    FakeHotStore,
    FakeUploadStore,
]:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    _seed(
        path,
        active_restore=active_restore,
        active_fetch=active_fetch,
        include_unrelated=include_unrelated,
    )
    failures = FailOnce(failure_point)
    archive_store = FakeArchiveStore(failures, include_unrelated=include_unrelated)
    hot_store = FakeHotStore(failures, include_unrelated=include_unrelated)
    upload_store = FakeUploadStore(failures)
    return (
        path,
        _service(path, archive_store, hot_store, upload_store),
        archive_store,
        hot_store,
        upload_store,
    )


def test_plan_enumerates_custody_impact_and_issues_state_bound_challenge(tmp_path: Path) -> None:
    path, service, _, hot_store, _ = _setup(tmp_path)

    plan = service.plan("2025/20250102T030405Z__docs")

    assert plan["status"] == "ready"
    assert "sole durable copies" in str(plan["warning"])
    assert str(plan["challenge"]).startswith("delete-")
    assert plan["file_count"] == 1
    assert plan["bytes"] == len(b"sole durable copy\n")
    assert plan["remote_storage_bytes"] == 130
    assert plan["metadata_rows"] == {
        "collections": 1,
        "collection_files": 1,
        "collection_archive_copies": 1,
        "archive_copy_jobs": 0,
        "collection_uploads": 1,
        "collection_upload_files": 1,
        "archive_restores": 1,
        "archive_restore_collections": 1,
        "encrypted_restore_catalog_entries": 1,
    }
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        assert session.get(CollectionDeletionRecord, "2025/20250102T030405Z__docs") is None

    hot_store.files[("2025/20250102T030405Z__docs", "second.txt")] = b"changed"
    with pytest.raises(Conflict, match="plan changed"):
        service.delete("2025/20250102T030405Z__docs", challenge=str(plan["challenge"]))


def test_collection_deletion_removes_every_archive_copy(tmp_path: Path) -> None:
    path, _, deep_store, hot_store, upload_store = _setup(tmp_path)
    factory = make_session_factory(sqlite_url(path))
    b2_paths = {
        "b2/archives/opaque-docs/archive.tar.age",
        "b2/archives/opaque-docs/manifest.yml.age",
        "b2/archives/opaque-docs/manifest.yml.ots.age",
    }
    with session_scope(factory) as session:
        session.add(
            CollectionArchiveCopyRecord(
                collection_id="2025/20250102T030405Z__docs",
                store="b2",
                state="uploaded",
                archive_storage_prefix="b2/archives/opaque-docs",
                object_path="b2/archives/opaque-docs/archive.tar.age",
                stored_bytes=200,
                sha256="d" * 64,
                manifest_object_path="b2/archives/opaque-docs/manifest.yml.age",
                manifest_sha256="e" * 64,
                manifest_stored_bytes=30,
                ots_object_path="b2/archives/opaque-docs/manifest.yml.ots.age",
                ots_sha256="f" * 64,
                ots_stored_bytes=10,
                last_verified_at="2026-07-15T00:00:00Z",
            )
        )
    b2_store = FakeArchiveStore(FailOnce())
    b2_store.objects = set(b2_paths)
    service = SqlAlchemyCollectionDeletionService(
        RuntimeConfig(database_url=sqlite_url(path)),
        ArchiveStoreRegistry(
            {"deep": deep_store, "b2": b2_store},
            default_store="deep",
        ),
        hot_store,
        upload_store,
    )

    plan = service.plan("2025/20250102T030405Z__docs")
    result = service.delete(
        "2025/20250102T030405Z__docs",
        challenge=str(plan["challenge"]),
    )

    assert result["status"] == "deleted"
    assert result["remote_storage_bytes"] == 370
    assert deep_store.catalog_entries == []
    assert b2_store.catalog_entries == []
    assert not (b2_paths & b2_store.objects)


def test_plan_reports_active_restore_and_fetch_blockers(tmp_path: Path) -> None:
    _, service, _, _, _ = _setup(tmp_path, active_restore=True, active_fetch=True)

    plan = service.plan("2025/20250102T030405Z__docs")

    assert plan["status"] == "blocked"
    assert plan["challenge"] is None
    assert plan["blockers"] == [
        "archive restore is active: ar-active",
        "fetch is active: fx-1",
    ]


def test_plan_blocks_collection_with_active_archive_copy(tmp_path: Path) -> None:
    path, service, _, _, _ = _setup(tmp_path)
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        session.add(
            ArchiveCopyJobRecord(
                collection_id="2025/20250102T030405Z__docs",
                source_store="deep",
                destination_store="b2",
                destination_storage_prefix="b2/archives/pending-copy",
                state="copying",
                requested_at="2026-07-15T00:00:00Z",
            )
        )

    plan = service.plan("2025/20250102T030405Z__docs")

    assert plan["status"] == "blocked"
    assert plan["challenge"] is None
    assert plan["blockers"] == ["archive copy is active: deep -> b2"]


def test_delete_removes_collection_objects_and_dependent_state(tmp_path: Path) -> None:
    path, service, archive_store, hot_store, upload_store = _setup(tmp_path)
    plan = service.plan("2025/20250102T030405Z__docs")

    result = service.delete("2025/20250102T030405Z__docs", challenge=str(plan["challenge"]))

    assert result == {
        "status": "deleted",
        "collection_id": "2025/20250102T030405Z__docs",
        "files": 1,
        "bytes": len(b"sole durable copy\n"),
        "remote_storage_bytes": 130,
    }
    assert archive_store.objects == set()
    assert archive_store.catalog_entries == []
    assert hot_store.files == {}
    assert upload_store.canceled == ["http://tusd.invalid/files/docs"]
    assert upload_store.deleted == [
        "/.riverhog/uploads/collections/2025/20250102T030405Z__docs/readme.txt"
    ]
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        assert session.get(CollectionRecord, "2025/20250102T030405Z__docs") is None
        assert session.get(CollectionUploadRecord, "2025/20250102T030405Z__docs") is None
        assert session.get(ArchiveRestoreRecord, "ar-complete") is None
        assert session.get(CollectionDeletionRecord, "2025/20250102T030405Z__docs") is None


@pytest.mark.parametrize(
    "failure_point",
    [
        "hot_object",
        "upload_resource",
        "upload_target",
        "archive_package",
        "recovery_catalog",
    ],
)
def test_delete_retries_after_each_external_boundary_failure(
    tmp_path: Path,
    failure_point: FailurePoint,
) -> None:
    path, service, archive_store, hot_store, _ = _setup(
        tmp_path,
        include_unrelated=True,
        failure_point=failure_point,
    )
    plan = service.plan("2025/20250102T030405Z__docs")
    challenge = str(plan["challenge"])

    with pytest.raises(SyntheticBoundaryFailure, match=failure_point):
        service.delete("2025/20250102T030405Z__docs", challenge=challenge)

    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        assert session.get(CollectionRecord, "2025/20250102T030405Z__docs") is not None
        assert session.get(CollectionRecord, "2025/20250103T030405Z__other") is not None
        assert session.get(CollectionDeletionRecord, "2025/20250102T030405Z__docs") is not None
    assert service.plan("2025/20250102T030405Z__docs")["challenge"] == challenge

    result = service.delete("2025/20250102T030405Z__docs", challenge=challenge)

    assert result["status"] == "deleted"
    assert hot_store.files == {
        ("2025/20250103T030405Z__other", "keep.txt"): b"unrelated durable copy\n"
    }
    assert archive_store.objects == {
        "archive/archives/opaque-other/archive.tar.age",
        "archive/archives/opaque-other/manifest.yml.age",
        "archive/archives/opaque-other/manifest.yml.ots.age",
    }
    assert archive_store.catalog_entries is not None
    assert [entry["collection_id"] for entry in archive_store.catalog_entries] == [
        "2025/20250103T030405Z__other"
    ]
    with session_scope(factory) as session:
        assert session.get(CollectionRecord, "2025/20250102T030405Z__docs") is None
        assert session.get(CollectionRecord, "2025/20250103T030405Z__other") is not None
        assert session.get(CollectionDeletionRecord, "2025/20250102T030405Z__docs") is None


def test_delete_retries_after_final_database_cleanup_failure(tmp_path: Path) -> None:
    path, service, archive_store, hot_store, _ = _setup(
        tmp_path,
        include_unrelated=True,
    )
    plan = service.plan("2025/20250102T030405Z__docs")
    challenge = str(plan["challenge"])
    fail_once = True

    def fail_after_cleanup_flush(session: Session, _: object) -> None:
        nonlocal fail_once
        if not fail_once or not any(
            isinstance(record, CollectionDeletionRecord) for record in session.deleted
        ):
            return
        fail_once = False
        raise SyntheticBoundaryFailure("synthetic deletion boundary failure: database_cleanup")

    event.listen(Session, "after_flush", fail_after_cleanup_flush)
    try:
        with pytest.raises(SyntheticBoundaryFailure, match="database_cleanup"):
            service.delete("2025/20250102T030405Z__docs", challenge=challenge)
    finally:
        event.remove(Session, "after_flush", fail_after_cleanup_flush)

    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        assert session.get(CollectionRecord, "2025/20250102T030405Z__docs") is not None
        assert session.get(CollectionRecord, "2025/20250103T030405Z__other") is not None
        assert session.get(CollectionDeletionRecord, "2025/20250102T030405Z__docs") is not None
    assert hot_store.files == {
        ("2025/20250103T030405Z__other", "keep.txt"): b"unrelated durable copy\n"
    }
    assert archive_store.catalog_entries is not None
    assert [entry["collection_id"] for entry in archive_store.catalog_entries] == [
        "2025/20250103T030405Z__other"
    ]

    result = service.delete("2025/20250102T030405Z__docs", challenge=challenge)

    assert result["status"] == "deleted"
    with session_scope(factory) as session:
        assert session.get(CollectionRecord, "2025/20250102T030405Z__docs") is None
        assert session.get(CollectionRecord, "2025/20250103T030405Z__other") is not None
        assert session.get(CollectionDeletionRecord, "2025/20250102T030405Z__docs") is None

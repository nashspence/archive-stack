from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ArchiveCopyJobRecord,
    ArchiveCopyRetirementRecord,
    ArchiveRestoreCollectionRecord,
    ArchiveRestoreRecord,
    ArchiveUsageSnapshotRecord,
    CollectionArchiveCopyRecord,
    CollectionDeletionRecord,
    CollectionRecord,
    FetchCollectionRecord,
    FetchRecord,
)
from riverhog_core.domain.errors import Conflict
from riverhog_core.ports.archive_store import (
    ArchivePackageVerificationError,
    CollectionArchivePackageIdentity,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_copy_retirements import (
    SqlAlchemyArchiveCopyRetirementService,
)
from riverhog_core.services.collection_custody import require_collection_custody_idle
from tests.unit.db_helpers import sqlite_url

_COLLECTION_ID = "2026/20260102T030405Z__docs"


class FakeArchiveStore:
    def __init__(self, name: str) -> None:
        self.name = name
        self.objects = {
            f"{name}/archives/opaque/archive.tar.age",
            f"{name}/archives/opaque/manifest.yml.age",
            f"{name}/archives/opaque/manifest.yml.ots.age",
        }
        self.verifications = 0
        self.fail_verification = False
        self.fail_catalog_once = False
        self.catalog_entries: list[dict[str, object]] | None = None

    def verify_collection_archive_package(
        self,
        *,
        collection_id: str,
        package: CollectionArchivePackageIdentity,
    ) -> None:
        assert collection_id == _COLLECTION_ID
        self.verifications += 1
        if self.fail_verification:
            raise ArchivePackageVerificationError("synthetic remote mismatch")
        assert package.archive.object_path in self.objects
        assert package.manifest.object_path in self.objects
        assert package.proof.object_path in self.objects

    def delete_collection_archive_package(
        self,
        *,
        collection_id: str,
        object_path: str,
        manifest_object_path: str,
        proof_object_path: str,
    ) -> None:
        assert collection_id == _COLLECTION_ID
        for path in (object_path, manifest_object_path, proof_object_path):
            self.objects.discard(path)

    def publish_restore_catalog(
        self,
        *,
        entries: list[dict[str, object]],
        generated_at: str,
    ) -> None:
        assert generated_at.endswith("Z")
        if self.fail_catalog_once:
            self.fail_catalog_once = False
            raise RuntimeError("synthetic catalog failure")
        self.catalog_entries = entries


def _copy(store: str, *, offset: int) -> CollectionArchiveCopyRecord:
    return CollectionArchiveCopyRecord(
        collection_id=_COLLECTION_ID,
        store=store,
        state="uploaded",
        archive_storage_prefix=f"{store}/archives/opaque",
        object_path=f"{store}/archives/opaque/archive.tar.age",
        stored_bytes=100 + offset,
        sha256=f"{offset + 1:x}" * 64,
        manifest_object_path=f"{store}/archives/opaque/manifest.yml.age",
        manifest_sha256=f"{offset + 2:x}" * 64,
        manifest_stored_bytes=20 + offset,
        ots_object_path=f"{store}/archives/opaque/manifest.yml.ots.age",
        ots_sha256=f"{offset + 3:x}" * 64,
        ots_stored_bytes=10 + offset,
        last_verified_at="2026-07-14T00:00:00Z",
    )


def _setup(
    tmp_path: Path,
) -> tuple[
    Path,
    SqlAlchemyArchiveCopyRetirementService,
    FakeArchiveStore,
    FakeArchiveStore,
]:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        session.add(CollectionRecord(id=_COLLECTION_ID))
        session.add(_copy("b2", offset=0))
        session.add(_copy("deep", offset=3))
    config = RuntimeConfig(database_url=sqlite_url(path))
    b2_config = replace(
        config.archive_store("deep"),
        name="b2",
        backend="b2",
        storage_class="STANDARD",
    )
    config = replace(
        config,
        archive_stores={"b2": b2_config, "deep": config.archive_store("deep")},
        archive_read_order=("b2", "deep"),
        default_archive_store="b2",
    )
    b2 = FakeArchiveStore("b2")
    deep = FakeArchiveStore("deep")
    return (
        path,
        SqlAlchemyArchiveCopyRetirementService(
            config,
            ArchiveStoreRegistry({"b2": b2, "deep": deep}, default_store="b2"),
        ),
        b2,
        deep,
    )


def test_plan_binds_exact_copy_and_retained_candidates(tmp_path: Path) -> None:
    path, service, _, _ = _setup(tmp_path)

    plan = service.plan(_COLLECTION_ID, store="b2")

    assert plan["status"] == "ready"
    assert "permanently removes one collection archive copy" in str(plan["warning"])
    assert str(plan["challenge"]).startswith("retire-copy-")
    assert plan["store"] == "b2"
    assert plan["target_copy"] == {
        "store": "b2",
        "last_verified_at": "2026-07-14T00:00:00Z",
        "remote_storage_bytes": 130,
        "objects": [
            {
                "kind": "archive",
                "object_path": "b2/archives/opaque/archive.tar.age",
                "stored_bytes": 100,
            },
            {
                "kind": "manifest",
                "object_path": "b2/archives/opaque/manifest.yml.age",
                "stored_bytes": 20,
            },
            {
                "kind": "proof",
                "object_path": "b2/archives/opaque/manifest.yml.ots.age",
                "stored_bytes": 10,
            },
        ],
    }
    assert [copy["store"] for copy in plan["retained_copies"]] == ["deep"]
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        assert session.get(ArchiveCopyRetirementRecord, (_COLLECTION_ID, "b2")) is None


def test_plan_blocks_retiring_the_last_complete_copy(tmp_path: Path) -> None:
    path, service, _, _ = _setup(tmp_path)
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        deep = session.get(CollectionArchiveCopyRecord, (_COLLECTION_ID, "deep"))
        assert deep is not None
        deep.state = "failed"

    plan = service.plan(_COLLECTION_ID, store="b2")

    assert plan["status"] == "blocked"
    assert plan["challenge"] is None
    assert plan["blockers"] == [
        "retirement would remove the collection's last complete archive copy"
    ]


def test_plan_reports_active_custody_work(tmp_path: Path) -> None:
    path, service, _, _ = _setup(tmp_path)
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        session.add(
            CollectionDeletionRecord(
                collection_id=_COLLECTION_ID,
                challenge="delete",
                plan_json="{}",
                started_at="2026-07-15T00:00:00Z",
            )
        )
        session.add(
            ArchiveRestoreRecord(
                restore_id="ar-active",
                state="requested",
                created_at="2026-07-15T00:00:00Z",
                retrieval_tier="standard",
                hold_days=2,
                warnings_json="[]",
            )
        )
        session.add(
            ArchiveRestoreCollectionRecord(
                restore_id="ar-active",
                collection_id=_COLLECTION_ID,
                archive_store="deep",
                collection_order=0,
            )
        )
        session.add(
            FetchRecord(
                fetch_id="fx-active",
                name="active fetch",
                fetch_order=1,
                fetch_state="restoring_archive",
            )
        )
        session.add(
            FetchCollectionRecord(
                fetch_id="fx-active",
                collection_id=_COLLECTION_ID,
                collection_order=0,
            )
        )
        session.add(
            ArchiveCopyJobRecord(
                collection_id=_COLLECTION_ID,
                source_store="deep",
                destination_store="third",
                destination_storage_prefix="third/archives/opaque",
                state="copying",
                requested_at="2026-07-15T00:00:00Z",
            )
        )
        session.add(
            ArchiveCopyRetirementRecord(
                collection_id=_COLLECTION_ID,
                store="deep",
                challenge="retire-copy",
                plan_json="{}",
                started_at="2026-07-15T00:00:00Z",
            )
        )

    plan = service.plan(_COLLECTION_ID, store="b2")

    assert plan["status"] == "blocked"
    assert plan["challenge"] is None
    assert plan["blockers"] == [
        f"collection deletion is active: {_COLLECTION_ID}",
        "archive restore is active: ar-active",
        "fetch is active: fx-active",
        "archive copy is active: deep -> third",
        "archive copy retirement is active: deep",
    ]


def test_active_retirement_reserves_collection_custody(tmp_path: Path) -> None:
    path, _, _, _ = _setup(tmp_path)
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        session.add(
            ArchiveCopyRetirementRecord(
                collection_id=_COLLECTION_ID,
                store="b2",
                challenge="retire-copy",
                plan_json="{}",
                started_at="2026-07-15T00:00:00Z",
            )
        )

    with session_scope(factory) as session:
        with pytest.raises(Conflict, match="archive copy retirement is in progress"):
            require_collection_custody_idle(session, _COLLECTION_ID)


def test_retire_verifies_another_store_and_updates_custody_state(tmp_path: Path) -> None:
    path, service, b2, deep = _setup(tmp_path)
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        session.add(
            ArchiveRestoreRecord(
                restore_id="ar-complete",
                state="completed",
                created_at="2026-07-14T00:00:00Z",
                retrieval_tier="standard",
                hold_days=2,
                warnings_json="[]",
            )
        )
        session.add(
            ArchiveRestoreCollectionRecord(
                restore_id="ar-complete",
                collection_id=_COLLECTION_ID,
                archive_store="b2",
                collection_order=0,
            )
        )
    plan = service.plan(_COLLECTION_ID, store="b2")

    result = service.retire(
        _COLLECTION_ID,
        store="b2",
        challenge=str(plan["challenge"]),
    )

    assert result == {
        "status": "retired",
        "collection_id": _COLLECTION_ID,
        "store": "b2",
        "remote_storage_bytes": 130,
        "verified_store": "deep",
    }
    assert deep.verifications == 1
    assert b2.objects == set()
    assert b2.catalog_entries == []
    assert deep.catalog_entries is not None
    assert [entry["collection_id"] for entry in deep.catalog_entries] == [_COLLECTION_ID]
    with session_scope(factory) as session:
        assert session.get(CollectionArchiveCopyRecord, (_COLLECTION_ID, "b2")) is None
        retained = session.get(CollectionArchiveCopyRecord, (_COLLECTION_ID, "deep"))
        assert retained is not None
        assert retained.last_verified_at != "2026-07-14T00:00:00Z"
        assert retained.last_verified_at is not None
        assert "." not in retained.last_verified_at
        assert session.get(ArchiveRestoreRecord, "ar-complete") is None
        assert session.get(ArchiveCopyRetirementRecord, (_COLLECTION_ID, "b2")) is None
        assert session.query(ArchiveUsageSnapshotRecord).count() == 1


def test_remote_verification_failure_keeps_target_and_clears_active_marker(
    tmp_path: Path,
) -> None:
    path, service, b2, deep = _setup(tmp_path)
    deep.fail_verification = True
    plan = service.plan(_COLLECTION_ID, store="b2")

    with pytest.raises(Conflict, match="no retained archive copy matches"):
        service.retire(
            _COLLECTION_ID,
            store="b2",
            challenge=str(plan["challenge"]),
        )

    assert len(b2.objects) == 3
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        assert session.get(CollectionArchiveCopyRecord, (_COLLECTION_ID, "b2")) is not None
        assert session.get(ArchiveCopyRetirementRecord, (_COLLECTION_ID, "b2")) is None


def test_retirement_resumes_after_selected_store_catalog_failure(tmp_path: Path) -> None:
    path, service, b2, _ = _setup(tmp_path)
    plan = service.plan(_COLLECTION_ID, store="b2")
    challenge = str(plan["challenge"])
    b2.fail_catalog_once = True

    with pytest.raises(RuntimeError, match="synthetic catalog failure"):
        service.retire(_COLLECTION_ID, store="b2", challenge=challenge)

    assert b2.objects == set()
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        assert session.get(ArchiveCopyRetirementRecord, (_COLLECTION_ID, "b2")) is not None
        assert session.get(CollectionArchiveCopyRecord, (_COLLECTION_ID, "b2")) is not None
    assert service.plan(_COLLECTION_ID, store="b2")["challenge"] == challenge

    result = service.retire(_COLLECTION_ID, store="b2", challenge=challenge)

    assert result["status"] == "retired"
    assert b2.catalog_entries == []
    with session_scope(factory) as session:
        assert session.get(CollectionArchiveCopyRecord, (_COLLECTION_ID, "b2")) is None

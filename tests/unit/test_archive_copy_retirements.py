from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import CollectionArchiveCopyRecord
from riverhog_core.services.archive_copy_retirements import (
    SqlAlchemyArchiveCopyRetirementService,
)
from riverhog_core.services.archive_records import apply_archive_receipt
from tests.unit.archive_object_fixtures import (
    COLLECTION_ID,
    MemoryArchiveStore,
    archive_receipt,
    as_archive_store,
    seed_archive_copy,
)

FILES = {"document.txt": b"archive copy retirement\n"}


def _service(path: Path):
    config, archive = seed_archive_copy(path, FILES, store="deep")
    with session_scope(make_session_factory(config.database_url)) as session:
        copy = CollectionArchiveCopyRecord(collection_id=COLLECTION_ID, store="b2")
        session.add(copy)
        session.flush()
        apply_archive_receipt(
            copy,
            archive_receipt(
                archive,
                backend="b2",
                storage_class="STANDARD",
                prefix="archives/b2/opaque-docs",
            ),
            archive,
        )
    b2 = replace(
        config.archive_store("deep"),
        name="b2",
        backend="b2",
        storage_class="STANDARD",
    )
    config = replace(
        config,
        archive_stores={"deep": config.archive_store("deep"), "b2": b2},
        archive_read_order=("b2", "deep"),
    )
    deep_store = MemoryArchiveStore(archive)
    b2_store = MemoryArchiveStore(archive, backend="b2")
    service = SqlAlchemyArchiveCopyRetirementService(
        config,
        ArchiveStoreRegistry(
            {"deep": as_archive_store(deep_store), "b2": as_archive_store(b2_store)},
        ),
    )
    return config, deep_store, b2_store, service


def test_retirement_plan_counts_the_target_objects(tmp_path: Path) -> None:
    _config, _deep, _b2, service = _service(tmp_path / "catalog.sqlite3")

    plan = service.plan(COLLECTION_ID, store="deep")

    assert plan["status"] == "ready"
    assert [current["kind"] for current in plan["target_copy"]["objects"]] == [
        "pack",
        "manifest",
        "proof",
    ]
    assert [current["store"] for current in plan["retained_copies"]] == ["b2"]
    assert plan["challenge"]


def test_retirement_verifies_a_retained_copy_then_deletes_every_target_object(
    tmp_path: Path,
) -> None:
    config, deep_store, b2_store, service = _service(tmp_path / "catalog.sqlite3")
    challenge = str(service.plan(COLLECTION_ID, store="deep")["challenge"])

    result = service.retire(COLLECTION_ID, store="deep", challenge=challenge)

    assert result["status"] == "retired"
    assert result["verified_store"] == "b2"
    assert b2_store.verified == [("data-000000", "manifest", "proof")]
    assert deep_store.deleted == [("data-000000", "manifest", "proof")]
    with session_scope(make_session_factory(config.database_url)) as session:
        assert session.get(CollectionArchiveCopyRecord, (COLLECTION_ID, "deep")) is None
        assert session.get(CollectionArchiveCopyRecord, (COLLECTION_ID, "b2")) is not None

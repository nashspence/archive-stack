from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import cast

from riverhog_api.schemas.archive import ArchiveCopyRetirementPlanOut
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveCopyRecord,
    CollectionMetadataPublicationRecord,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_copy_retirements import (
    SqlAlchemyArchiveCopyRetirementService,
)

from tests.unit.archive_object_fixtures import (
    COLLECTION_ID,
    MemoryArchiveStore,
    add_archive_copy,
    archive_store_binding,
    seed_archive_copy,
)

FILES = {"document.txt": b"archive copy retirement\n"}


def _service(
    path: Path,
) -> tuple[
    RuntimeConfig,
    MemoryArchiveStore,
    MemoryArchiveStore,
    SqlAlchemyArchiveCopyRetirementService,
]:
    config, archive = seed_archive_copy(path, FILES, store="deep")
    with session_scope(make_session_factory(config.database_url)) as session:
        add_archive_copy(
            session,
            archive,
            store="b2",
            backend="b2",
            storage_class="STANDARD",
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
            {"deep": archive_store_binding(deep_store), "b2": archive_store_binding(b2_store)},
        ),
    )
    return config, deep_store, b2_store, service


def test_retirement_plan_counts_the_target_objects(tmp_path: Path) -> None:
    _config, _deep, _b2, service = _service(tmp_path / "catalog.sqlite3")

    plan = service.plan(COLLECTION_ID, store="deep")

    assert plan["status"] == "ready"
    target = cast(dict[str, object], plan["target_copy"])
    retained = cast(list[dict[str, object]], plan["retained_copies"])
    assert target["object_count"] == 3
    assert [current["store"] for current in retained] == ["b2"]
    assert plan["retired_retrieval_job_count"] == 0
    assert plan["challenge"]
    ArchiveCopyRetirementPlanOut.model_validate(plan)


def test_active_target_metadata_publication_blocks_retirement(tmp_path: Path) -> None:
    config, _deep, _b2, service = _service(tmp_path / "catalog.sqlite3")
    with session_scope(make_session_factory(config.database_url)) as session:
        session.add(
            CollectionMetadataPublicationRecord(
                collection_id=COLLECTION_ID,
                store="deep",
                desired_revision=1,
                state="publishing",
                attempt_count=1,
                next_attempt_at="2026-07-15T00:00:00.000000Z",
                last_attempt_at="2026-07-15T00:00:00.000000Z",
            )
        )

    plan = service.plan(COLLECTION_ID, store="deep")

    assert plan["status"] == "blocked"
    assert plan["challenge"] is None
    assert plan["blockers"] == ["collection metadata publication is active: deep"]


def test_retirement_verifies_a_retained_copy_then_deletes_every_target_object(
    tmp_path: Path,
) -> None:
    config, deep_store, b2_store, service = _service(tmp_path / "catalog.sqlite3")
    challenge = str(service.plan(COLLECTION_ID, store="deep")["challenge"])

    result = service.retire(COLLECTION_ID, store="deep", challenge=challenge)

    assert result["status"] == "retired"
    assert result["verified_store"] == "b2"
    assert b2_store.verified == [("pack-000000000000", "manifest", "proof")]
    assert deep_store.deleted == [("pack-000000000000", "manifest", "proof")]
    with session_scope(make_session_factory(config.database_url)) as session:
        assert session.get(CollectionArchiveCopyRecord, (COLLECTION_ID, "deep")) is None
        assert session.get(CollectionArchiveCopyRecord, (COLLECTION_ID, "b2")) is not None

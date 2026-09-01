from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import cast

from riverhog_api.schemas.archive import ArchiveCopyRetirementPlanOut
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveCopyRecord,
    CollectionArchiveObjectRecord,
    CollectionMetadataPublicationRecord,
    RetrievalPlanObjectRecord,
    RetrievalPlanRecord,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_copy_retirements import (
    SqlAlchemyArchiveCopyRetirementService,
)
from sqlalchemy import select

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
        )
    b2 = replace(
        config.archive_store("deep"),
        name="b2",
        base_url="http://127.0.0.1/b2",
    )
    config = replace(
        config,
        archive_stores={"deep": config.archive_store("deep"), "b2": b2},
        archive_read_order=("b2", "deep"),
    )
    deep_store = MemoryArchiveStore(archive)
    b2_store = MemoryArchiveStore(archive, new_archive_prefix="archives/b2/new-copy")
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
    assert target["object_count"] == 5
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
    expected = (
        "pack-" + "0" * 64,
        "volume-metadata-" + "0" * 64,
        "volume-terminal-" + "0" * 63 + "1",
        "manifest",
        "recovery-descriptor",
    )
    assert b2_store.verified == [expected]
    assert deep_store.deleted == [expected]
    with session_scope(make_session_factory(config.database_url)) as session:
        assert session.get(CollectionArchiveCopyRecord, (COLLECTION_ID, "deep")) is None
        assert session.get(CollectionArchiveCopyRecord, (COLLECTION_ID, "b2")) is not None


def test_retirement_blocks_an_active_plan_and_reclaims_its_expired_authority(
    tmp_path: Path,
) -> None:
    config, _deep_store, _b2_store, service = _service(tmp_path / "catalog.sqlite3")
    plan_id = "retrieval-plan"
    with session_scope(make_session_factory(config.database_url)) as session:
        archive_object = session.scalar(
            select(CollectionArchiveObjectRecord)
            .where(
                CollectionArchiveObjectRecord.collection_id == COLLECTION_ID,
                CollectionArchiveObjectRecord.store == "deep",
                CollectionArchiveObjectRecord.kind.in_({"pack", "segment"}),
            )
            .order_by(CollectionArchiveObjectRecord.object_id)
            .limit(1)
        )
        assert archive_object is not None
        session.add(
            RetrievalPlanRecord(
                id=plan_id,
                app="reader",
                initiated_by_key_id="key",
                idempotency_key=plan_id,
                creation_identity_sha256="d" * 64,
                state="ready",
                request_json='[{"collection_id":1,"path":"document.txt"}]',
                lease_seconds=3600,
                restore_policy="allow",
                created_at="2026-08-08T00:00:00.000000Z",
                ready_at="2026-08-08T00:00:00.000000Z",
                expires_at="2099-08-08T00:00:00.000000Z",
                failure=None,
                next_file_order=1,
                next_placement_sequence=0,
                object_count=1,
                retrieval_bytes=archive_object.stored_bytes,
                requires_restore=False,
                file_commitment_sha256="a" * 64,
                segment_commitment_sha256="b" * 64,
                etag="c" * 64,
            )
        )
        session.add(
            RetrievalPlanObjectRecord(
                plan_id=plan_id,
                object_order=0,
                collection_id=COLLECTION_ID,
                source_store="deep",
                object_id=archive_object.object_id,
                kind=archive_object.kind,
                plaintext_bytes=archive_object.plaintext_bytes,
                stored_bytes=archive_object.stored_bytes,
                sha256=archive_object.sha256,
                read_mode="immediate",
                cache_store=None,
                retrieval_bytes=archive_object.stored_bytes,
            )
        )

    blocked = service.plan(COLLECTION_ID, store="deep")
    assert blocked["status"] == "blocked"
    assert blocked["blockers"] == [f"retrieval plan is active: {plan_id}"]

    with session_scope(make_session_factory(config.database_url)) as session:
        plan = session.get(RetrievalPlanRecord, plan_id)
        assert plan is not None
        plan.expires_at = "2020-08-08T00:00:00.000000Z"

    ready = service.plan(COLLECTION_ID, store="deep")
    assert ready["status"] == "ready"
    result = service.retire(
        COLLECTION_ID,
        store="deep",
        challenge=str(ready["challenge"]),
    )

    assert result["status"] == "retired"
    with session_scope(make_session_factory(config.database_url)) as session:
        assert session.get(RetrievalPlanRecord, plan_id) is None

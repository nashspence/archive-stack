from __future__ import annotations

from pathlib import Path

import pytest
from riverhog_api.schemas.collections import CollectionDeletionPlanOut
from riverhog_application_access import ALL_RESOURCES, CATALOG_READ, ApplicationAccess
from riverhog_core.app_permissions import ApplicationPrincipal
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CatalogEventAccessGroupRecord,
    CatalogEventRecord,
    CatalogSyncStateRecord,
    CollectionAccessGroupMembershipRecord,
    CollectionAccessGroupRecord,
    CollectionArchiveObjectRecord,
    CollectionFileRecord,
    CollectionRecord,
    RetrievalCacheLeaseRecord,
    RetrievalCacheObjectRecord,
    RetrievalCacheStoreAccountingRecord,
    RetrievalPlanFileRecord,
    RetrievalPlanRecord,
)
from riverhog_core.services.catalog_sync import SqlAlchemyCatalogSyncService
from riverhog_core.services.collection_deletions import (
    SqlAlchemyCollectionDeletionService,
    _bounded_blocker_sample,
)
from riverhog_core.services.lifecycle_events import SqlAlchemyLifecycleEventService
from riverhog_protocol.transport import (
    COLLECTION_DELETION_BLOCKER_CATEGORY_SAMPLE_MAX,
    COLLECTION_DELETION_BLOCKERS_MAX,
)

from tests.unit.archive_object_fixtures import (
    COLLECTION_ID,
    UPLOADED_AT,
    MemoryArchiveStore,
    archive_store_binding,
    seed_archive_copy,
)

FILES = {"one.txt": b"first file\n", "two.txt": b"second file\n"}
DELETER = ApplicationPrincipal(
    app="riverhog-client",
    key_id="client-key",
    access=frozenset(),
)
READER = ApplicationPrincipal(
    app="indexer",
    key_id="indexer-key",
    access=frozenset({ApplicationAccess(CATALOG_READ, ALL_RESOURCES)}),
)


def test_deletion_blocker_samples_report_overflow_within_the_public_bound() -> None:
    values = list(range(COLLECTION_DELETION_BLOCKER_CATEGORY_SAMPLE_MAX + 1))

    rendered = _bounded_blocker_sample(
        values,
        render=lambda value: f"blocker {value}",
        overflow="additional blockers exist",
    )

    assert rendered[:-1] == [
        f"blocker {value}" for value in range(COLLECTION_DELETION_BLOCKER_CATEGORY_SAMPLE_MAX)
    ]
    assert rendered[-1] == "additional blockers exist"
    schema = CollectionDeletionPlanOut.model_json_schema()["properties"]["blockers"]
    assert schema["maxItems"] == COLLECTION_DELETION_BLOCKERS_MAX
    assert COLLECTION_DELETION_BLOCKERS_MAX == 5 * len(rendered)


def _service(path: Path, *, retrieval_cache: object | None = None):
    config, archive = seed_archive_copy(path, FILES)
    archive_store = MemoryArchiveStore(archive)
    service = SqlAlchemyCollectionDeletionService(
        config,
        ArchiveStoreRegistry({"deep": archive_store_binding(archive_store)}),
        retrieval_cache,  # type: ignore[arg-type]
    )
    return config, archive_store, service


class _Cache:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.raise_after_delete = False

    def delete(self, *, cache_store: str, object_path: str, revision: str | None) -> None:
        assert cache_store == "local"
        assert revision == "cache-revision"
        self.deleted.append(object_path)
        if self.raise_after_delete:
            self.raise_after_delete = False
            raise RuntimeError("ambiguous cache deletion")


def _drain(service: SqlAlchemyCollectionDeletionService) -> int:
    progressed = 0
    while current := service.process_due(limit=1):
        progressed += current
    return progressed


def test_deletion_plan_uses_catalog_object_and_file_aggregates(tmp_path: Path) -> None:
    _config, _archive_store, service = _service(tmp_path / "catalog.sqlite3")

    plan = service.plan(COLLECTION_ID)

    assert plan["status"] == "ready"
    assert plan["file_count"] == 2
    assert plan["bytes"] == sum(map(len, FILES.values()))
    assert plan["archive_object_count"] == 5
    assert plan["archive_copies"] == [
        {
            "store": "deep",
            "objects": 5,
            "stored_bytes": plan["remote_storage_bytes"],
        }
    ]
    assert plan["upload_file_count"] == 0
    CollectionDeletionPlanOut.model_validate(plan)


def test_confirmed_deletion_removes_archive_and_catalog_record(
    tmp_path: Path,
) -> None:
    config, archive_store, service = _service(tmp_path / "catalog.sqlite3")
    challenge = str(service.plan(COLLECTION_ID)["challenge"])

    result = service.delete(COLLECTION_ID, challenge=challenge, initiator=DELETER)

    assert result["status"] == "deleting"
    with session_scope(make_session_factory(config.database_url)) as session:
        collection = session.get(CollectionRecord, COLLECTION_ID)
        assert collection is not None and collection.is_published is False
    assert _drain(service) == 9
    assert archive_store.deleted == [
        ("pack-" + "0" * 64,),
        ("volume-metadata-" + "0" * 64,),
        ("volume-terminal-" + "0" * 63 + "1",),
        ("manifest",),
        ("recovery-descriptor",),
    ]
    with session_scope(make_session_factory(config.database_url)) as session:
        assert session.get(CollectionRecord, COLLECTION_ID) is None
        event = session.query(CatalogEventRecord).one()
        assert event.change == "deleted" and event.collection_id == COLLECTION_ID
        assert session.query(CatalogEventAccessGroupRecord).count() == 0


def test_deletion_event_belongs_to_the_authenticated_deleter_across_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, archive_store, service = _service(tmp_path / "catalog.sqlite3")
    with session_scope(make_session_factory(config.database_url)) as session:
        collection = session.get(CollectionRecord, COLLECTION_ID)
        assert collection is not None
        collection.created_by_app = "stove0"
        collection.created_by_key_id = "stove0-key"

    original_delete = archive_store.delete_collection_archive
    attempts = 0

    def fail_once(**kwargs: object) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("provider unavailable")
        original_delete(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(archive_store, "delete_collection_archive", fail_once)
    challenge = str(service.plan(COLLECTION_ID)["challenge"])
    started = service.delete(
        COLLECTION_ID,
        challenge=challenge,
        initiator=DELETER,
        event_context={"workflow": "direct-delete"},
    )
    assert started["status"] == "deleting"
    with pytest.raises(RuntimeError, match="provider unavailable"):
        service.process_due(limit=1)

    active = service.plan(COLLECTION_ID)
    assert active["status"] == "deleting"
    assert "_execution" not in active
    retrying_app = ApplicationPrincipal(
        app="stove0",
        key_id="stove0-key",
        access=frozenset(),
    )
    result = service.delete(
        COLLECTION_ID,
        challenge=challenge,
        initiator=retrying_app,
        event_context={"workflow": "retry"},
    )

    assert result["status"] == "deleting"
    assert _drain(service) == 9
    events = SqlAlchemyLifecycleEventService(config)
    page = events.page(owner_app="riverhog-client", after=None, limit=100)
    assert len(page.events) == 1
    event = page.events[0]
    assert event.type == "io.riverhog.riverhog.collection.deleted"
    assert event.data["actor"] == {"app": "riverhog"}
    assert event.data["initiator"] == {
        "app": "riverhog-client",
        "key_id": "client-key",
    }
    assert event.data["collection_created_at"] == UPLOADED_AT
    assert event.data["context"] == {"workflow": "direct-delete"}
    assert events.page(owner_app="stove0", after=None, limit=100).events == []


def test_catalog_teardown_is_bounded_and_event_publishes_only_when_complete(
    tmp_path: Path,
) -> None:
    config, archive_store, service = _service(tmp_path / "catalog.sqlite3")
    catalog_sync = SqlAlchemyCatalogSyncService(config)
    checkpoint = catalog_sync.checkpoint(principal=READER)
    factory = make_session_factory(config.database_url)
    with session_scope(factory) as session:
        for index in range(205):
            group_id = f"{index:064x}"
            session.add(
                CollectionAccessGroupRecord(
                    id=group_id,
                    creation_idempotency_key=f"bulk-{index:03d}",
                    created_by_app="fixture",
                    display_label=f"bulk-{index:03d}",
                    status="active",
                    authorization_revision=1,
                    created_at=UPLOADED_AT,
                    updated_at=UPLOADED_AT,
                    collection_count=1,
                )
            )
            session.add(
                CollectionAccessGroupMembershipRecord(
                    collection_id=COLLECTION_ID,
                    group_id=group_id,
                    added_by_app="fixture",
                    added_at=UPLOADED_AT,
                )
            )
            session.add(
                CollectionFileRecord(
                    collection_id=COLLECTION_ID,
                    path=f"bulk/{index:03d}.bin",
                    bytes=1,
                    sha256=f"{index:064x}",
                )
            )

    challenge = str(service.plan(COLLECTION_ID)["challenge"])
    service.delete(COLLECTION_ID, challenge=challenge, initiator=DELETER)
    bootstrap = catalog_sync.collections(
        cursor=checkpoint.catalog_cursor,
        limit=100,
        principal=READER,
    )
    assert bootstrap.collections == []
    assert bootstrap.changes_cursor is not None
    initial = catalog_sync.changes(
        cursor=bootstrap.changes_cursor,
        limit=100,
        principal=READER,
    )
    assert initial.changes == [] and initial.caught_up is True
    previous_groups = 205
    previous_files = 207
    steps = 0
    while service.process_due(limit=1):
        steps += 1
        with session_scope(factory) as session:
            group_count = session.query(CollectionAccessGroupMembershipRecord).count()
            file_count = session.query(CollectionFileRecord).count()
            assert 0 <= previous_groups - group_count <= 100
            assert 0 <= previous_files - file_count <= 100
            previous_groups = group_count
            previous_files = file_count
            collection = session.get(CollectionRecord, COLLECTION_ID)
            event = session.query(CatalogEventRecord).one_or_none()
            event_unpublished = event is not None and not event.published
            if event is not None:
                assert event.published is (collection is None)
        if event_unpublished:
            with session_scope(factory) as session:
                state = session.get(CatalogSyncStateRecord, 1)
                assert state is not None and state.committed_revision == 0

    assert steps > 9
    assert previous_groups == 0
    assert previous_files == 0
    with session_scope(factory) as session:
        event = session.query(CatalogEventRecord).one()
        assert event.published is True
        event_revision = event.revision
        assert session.query(CatalogEventAccessGroupRecord).count() == 205
    changes = catalog_sync.changes(cursor=initial.next_cursor, limit=100, principal=READER)
    assert changes.caught_up is True
    assert changes.through_revision == str(event_revision)
    assert [current.operation for current in changes.changes] == ["delete"]


def test_deletion_reclaims_a_multi_collection_retrieval_plan_as_one_authority(
    tmp_path: Path,
) -> None:
    config, _archive_store, service = _service(tmp_path / "catalog.sqlite3")
    factory = make_session_factory(config.database_url)
    other_collection_id = COLLECTION_ID + 1
    with session_scope(factory) as session:
        session.add(
            CollectionRecord(
                id=other_collection_id,
                creation_idempotency_key="other-collection",
                creation_identity_sha256="1" * 64,
                creation_custody_mode="producer-retained",
                content_identity="2" * 64,
                encryption_format="age-v1-scrypt",
                passphrase_id="default",
                provenance_mode="omitted",
                provenance_identity=None,
                inventory_identity="4" * 64,
                created_at=UPLOADED_AT,
                file_count=1,
                file_bytes=1,
            )
        )
        session.add(
            CollectionFileRecord(
                collection_id=other_collection_id,
                path="other.bin",
                bytes=1,
                sha256="5" * 64,
            )
        )
        session.add(
            RetrievalPlanRecord(
                id="multi-collection-plan",
                app="reader",
                idempotency_key="multi-collection-plan",
                creation_identity_sha256="8" * 64,
                state="expired",
                request_json=(
                    f'[{{"collection_id":{COLLECTION_ID},"path":"one.txt"}},'
                    f'{{"collection_id":{other_collection_id},"path":"other.bin"}}]'
                ),
                lease_seconds=3600,
                restore_policy="allow",
                created_at=UPLOADED_AT,
                expires_at=UPLOADED_AT,
                file_commitment_sha256="6" * 64,
                segment_commitment_sha256="7" * 64,
            )
        )
        target_file = session.get(CollectionFileRecord, (COLLECTION_ID, "one.txt"))
        assert target_file is not None
        session.add_all(
            (
                RetrievalPlanFileRecord(
                    plan_id="multi-collection-plan",
                    file_order=0,
                    collection_id=COLLECTION_ID,
                    path="one.txt",
                    bytes=len(FILES["one.txt"]),
                    sha256=target_file.sha256,
                    source_store="deep",
                ),
                RetrievalPlanFileRecord(
                    plan_id="multi-collection-plan",
                    file_order=1,
                    collection_id=other_collection_id,
                    path="other.bin",
                    bytes=1,
                    sha256="5" * 64,
                    source_store="deep",
                ),
            )
        )

    assert service._delete_retrieval_references(COLLECTION_ID) is True

    with session_scope(factory) as session:
        assert session.get(RetrievalPlanRecord, "multi-collection-plan") is None
        assert session.get(CollectionRecord, other_collection_id) is not None
        assert session.get(CollectionFileRecord, (other_collection_id, "other.bin")) is not None


def test_cache_deletion_waits_for_lease_and_accounts_once_after_ambiguous_response(
    tmp_path: Path,
) -> None:
    cache = _Cache()
    config, _archive_store, service = _service(
        tmp_path / "catalog.sqlite3",
        retrieval_cache=cache,
    )
    factory = make_session_factory(config.database_url)
    with session_scope(factory) as session:
        objects = list(
            session.query(CollectionArchiveObjectRecord)
            .order_by(CollectionArchiveObjectRecord.object_order)
            .limit(2)
        )
        assert len(objects) == 2
        for index, current in enumerate(objects):
            session.add(
                RetrievalCacheObjectRecord(
                    source_store=current.store,
                    collection_id=current.collection_id,
                    object_id=current.object_id,
                    cache_store="local",
                    object_path=f"cache/{index}",
                    revision="cache-revision",
                    stored_bytes=11 + index,
                    stored_sha256=None,
                    cached_at=UPLOADED_AT,
                    verified_at=UPLOADED_AT,
                    state="ready",
                )
            )
        first_identity = (objects[0].store, objects[0].object_id)
        session.add(
            RetrievalCacheStoreAccountingRecord(
                cache_store="local",
                reserved_bytes=0,
                committed_bytes=23,
                updated_at=UPLOADED_AT,
            )
        )
        session.flush()
        session.add(
            RetrievalCacheLeaseRecord(
                owner="active-reader",
                source_store=first_identity[0],
                collection_id=COLLECTION_ID,
                object_id=first_identity[1],
                expires_at="2099-01-01T00:00:00.000000Z",
            )
        )

    challenge = str(service.plan(COLLECTION_ID)["challenge"])
    assert service.delete(COLLECTION_ID, challenge=challenge, initiator=DELETER)["status"] == (
        "deleting"
    )
    assert service.process_due(limit=1) == 1
    with session_scope(factory) as session:
        accounting = session.get(RetrievalCacheStoreAccountingRecord, "local")
        assert accounting is not None and accounting.committed_bytes == 11
        assert session.query(RetrievalCacheObjectRecord).count() == 1
    assert service.process_due(limit=1) == 0

    with session_scope(factory) as session:
        lease = session.get(
            RetrievalCacheLeaseRecord,
            ("active-reader", first_identity[0], COLLECTION_ID, first_identity[1]),
        )
        assert lease is not None
        session.delete(lease)
    cache.raise_after_delete = True
    with pytest.raises(RuntimeError, match="ambiguous cache deletion"):
        service.process_due(limit=1)
    with session_scope(factory) as session:
        accounting = session.get(RetrievalCacheStoreAccountingRecord, "local")
        remaining = session.query(RetrievalCacheObjectRecord).one()
        assert accounting is not None and accounting.committed_bytes == 11
        assert remaining.state == "delete_pending"

    assert service.process_due(limit=1) == 1
    with session_scope(factory) as session:
        accounting = session.get(RetrievalCacheStoreAccountingRecord, "local")
        assert accounting is not None and accounting.committed_bytes == 0
        assert session.query(RetrievalCacheObjectRecord).count() == 0
    assert cache.deleted == ["cache/1", "cache/0", "cache/0"]

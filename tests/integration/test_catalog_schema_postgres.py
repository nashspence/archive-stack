from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from riverhog_age import CHUNK_SIZE
from riverhog_core.app_permissions import (
    ALL_RESOURCES,
    CATALOG_READ,
    COLLECTIONS_CREATE,
    ApplicationAccess,
    ApplicationPrincipal,
)
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import (
    catalog_state_schema,
    create_catalog_engine,
    initialize_db,
    make_session_factory,
    session_scope,
    validate_db,
)
from riverhog_core.catalog_events import record_catalog_event
from riverhog_core.catalog_models import (
    CatalogEventRecord,
    CatalogSyncStateRecord,
    CollectionArchiveObjectUploadRecord,
    CollectionRecord,
    CollectionTagNodeRecord,
    CollectionUploadProvenanceArchiveVolumeRecord,
    CollectionUploadRecord,
    RetrievalPlanObjectRecord,
    RetrievalPlanPlacementRecord,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.catalog_sync import SqlAlchemyCatalogSyncService
from riverhog_core.services.collection_uploads import SqlAlchemyCollectionUploadService
from riverhog_core.services.retrieval import SqlAlchemyRetrievalService
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from tests.unit.archive_object_fixtures import (
    MemoryArchiveStore,
    archive_store_binding,
)
from tests.unit.test_retrieval_service import _seed_collection

pytestmark = pytest.mark.integration
V1_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/state/v1_0001/riverhog.postgresql.sql"


@pytest.fixture
def isolated_database_url() -> Iterator[str]:
    value = os.getenv("RIVERHOG_TEST_POSTGRES_URL", "").strip()
    if not value:
        pytest.skip("RIVERHOG_TEST_POSTGRES_URL is required")
    schema = f"riverhog_catalog_{uuid4().hex}"
    admin_engine = create_catalog_engine(value)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    url = make_url(value).update_query_dict({"options": f"-csearch_path={schema},public"})
    try:
        yield url.render_as_string(hide_password=False)
    finally:
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


def test_postgres_catalog_schema_is_current_and_stays_operator_controlled(
    isolated_database_url: str,
) -> None:
    upgraded = initialize_db(isolated_database_url)
    engine = create_catalog_engine(isolated_database_url)
    before = {index["name"] for index in inspect(engine).get_indexes("retrieval_jobs")}

    validated = validate_db(isolated_database_url)

    after = {index["name"] for index in inspect(engine).get_indexes("retrieval_jobs")}
    assert upgraded.condition == validated.condition == "current"
    assert upgraded.current_revision == validated.current_revision == "v1_0001"
    assert after == before
    engine.dispose()


def test_postgres_current_v1_fixture_validates_and_restarts(
    isolated_database_url: str,
) -> None:
    engine = create_catalog_engine(isolated_database_url)
    fixture_sql = V1_FIXTURE.read_text(encoding="utf-8")
    with engine.begin() as connection:
        for statement in fixture_sql.split(";\n"):
            if statement.strip():
                connection.exec_driver_sql(statement)
    status = catalog_state_schema(isolated_database_url).validate()
    engine.dispose()

    restarted = create_catalog_engine(isolated_database_url)
    with restarted.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO collection_tags "
                "(tag_sha256, tag, search_text, created_at, updated_at, collection_count) "
                "VALUES (:id, 'fixture', 'fixture', "
                "'2026-01-01T00:00:00.000000Z', "
                "'2026-01-01T00:00:00.000000Z', 0)"
            ),
            {"id": "0" * 64},
        )
    restarted.dispose()

    assert status.condition == "current"
    assert status.current_revision == "v1_0001"
    assert validate_db(isolated_database_url).condition == "current"


def test_postgres_retrieval_plan_advances_in_bounded_restartable_steps(
    isolated_database_url: str,
    tmp_path: Path,
) -> None:
    segment_count = 65
    content = bytes(index % 251 for index in range(segment_count * CHUNK_SIZE))
    service, collection_id, _ranges, _store = _seed_collection(
        tmp_path,
        {"many-segments.bin": content},
        database_url=isolated_database_url,
        raw=True,
        raw_volume_plaintext_bytes=CHUNK_SIZE,
        raw_part_plaintext_bytes=CHUNK_SIZE,
    )

    plan = service.plan(((collection_id, "many-segments.bin"),))
    assert plan["state"] == "planning"
    with session_scope(service._session_factory) as session:
        assert len(session.scalars(select(RetrievalPlanObjectRecord)).all()) == 32
        assert len(session.scalars(select(RetrievalPlanPlacementRecord)).all()) == 32

    restarted = SqlAlchemyRetrievalService(
        service._config,
        service._archive_stores,
        service._cache,
        session_factory=make_session_factory(isolated_database_url),
    )
    plan = restarted.advance_plan(app="", plan_id=str(plan["id"]))
    assert plan["state"] == "planning"
    plan = restarted.advance_plan(app="", plan_id=str(plan["id"]))
    assert plan["state"] == "ready"
    with session_scope(make_session_factory(isolated_database_url)) as session:
        assert len(session.scalars(select(RetrievalPlanObjectRecord)).all()) == segment_count
        assert len(session.scalars(select(RetrievalPlanPlacementRecord)).all()) == segment_count


def test_postgres_upload_idempotency_is_independent_per_application(
    isolated_database_url: str,
) -> None:
    initialize_db(isolated_database_url)
    access = frozenset({ApplicationAccess(COLLECTIONS_CREATE, ALL_RESOURCES)})
    memory_store = MemoryArchiveStore()
    archive_stores = ArchiveStoreRegistry({"archive": archive_store_binding(memory_store)})

    def create(app: str, key_id: str) -> dict[str, object]:
        service = SqlAlchemyCollectionUploadService(
            RuntimeConfig(database_url=isolated_database_url),
            archive_stores,
        )
        return service.create_or_resume(
            idempotency_key="shared-retry-key",
            ingest_source="postgres-fixture",
            archive_store=None,
            initiator=ApplicationPrincipal(app=app, key_id=key_id, access=access),
            event_context=None,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(create, "first", "first-key")
        second_future = executor.submit(create, "second", "second-key")
        first = first_future.result()
        second = second_future.result()

    rotated = create("first", "replacement-key")
    assert first["collection_id"] != second["collection_id"]
    assert rotated["collection_id"] == first["collection_id"]

    engine = create_catalog_engine(isolated_database_url)
    assert {
        tuple(str(column) for column in constraint["column_names"])
        for constraint in inspect(engine).get_unique_constraints("collections")
    } == {("created_by_app", "creation_idempotency_key")}
    indexes = {
        str(index["name"]): index for index in inspect(engine).get_indexes("collection_uploads")
    }
    idempotency_index = indexes["ux_collection_uploads_application_idempotency_key"]
    assert idempotency_index["column_names"] == ["initiated_by_app", "idempotency_key"]
    assert idempotency_index["unique"] is True
    engine.dispose()


def test_postgres_catalog_revisions_serialize_commit_and_restart(
    isolated_database_url: str,
) -> None:
    initialize_db(isolated_database_url)
    factory = make_session_factory(isolated_database_url)
    with session_scope(factory) as session:
        for collection_id in (1, 2):
            identity = f"{collection_id:064x}"
            session.add(
                CollectionRecord(
                    id=collection_id,
                    creation_idempotency_key=f"catalog-sync-{collection_id}",
                    creation_identity_sha256=identity,
                    creation_custody_mode="producer-retained",
                    archive_generation=identity,
                    content_identity=identity,
                    encryption_format="age-v1-scrypt",
                    passphrase_id="fixture",
                    provenance_mode="omitted",
                    provenance_identity=None,
                    inventory_identity=identity,
                    archive_root_sha256=identity,
                    created_by_app="fixture",
                    created_at="2026-09-07T00:00:00.000000Z",
                    is_published=True,
                    file_count=0,
                    file_bytes=0,
                )
            )

    first_locked = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()

    def publish(collection_id: int) -> int:
        with session_scope(make_session_factory(isolated_database_url)) as session:
            if collection_id == 2:
                second_started.set()
            event = record_catalog_event(
                session,
                change="created",
                collection_id=collection_id,
                occurred_at="2026-09-07T00:00:00.000000Z",
                inventory_identity=f"{collection_id:064x}",
                before_tags=(),
                after_tags=(),
            )
            if collection_id == 1:
                first_locked.set()
                assert release_first.wait(timeout=5)
            assert event.revision is not None
            return event.revision

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(publish, 1)
        assert first_locked.wait(timeout=5)
        second = executor.submit(publish, 2)
        assert second_started.wait(timeout=5)
        release_first.set()
        assert (first.result(timeout=5), second.result(timeout=5)) == (1, 2)

    with session_scope(make_session_factory(isolated_database_url)) as session:
        state = session.get(CatalogSyncStateRecord, 1)
        assert state is not None and state.committed_revision == 2
        assert list(
            session.scalars(
                select(CatalogEventRecord.revision).order_by(CatalogEventRecord.revision)
            )
        ) == [1, 2]

    restarted = SqlAlchemyCatalogSyncService(
        RuntimeConfig(database_url=isolated_database_url),
        session_factory=make_session_factory(isolated_database_url),
    )
    reader = ApplicationPrincipal(
        app="indexer",
        key_id="indexer-key",
        access=frozenset({ApplicationAccess(CATALOG_READ, ALL_RESOURCES)}),
    )
    checkpoint = restarted.checkpoint(principal=reader)
    page = restarted.collections(cursor=checkpoint.catalog_cursor, limit=100, principal=reader)
    assert [item.collection_id for item in page.collections] == [1, 2]


def test_postgres_tag_history_cleanup_serializes_its_row_work_budget(
    isolated_database_url: str,
) -> None:
    initialize_db(isolated_database_url)
    factory = make_session_factory(isolated_database_url)
    with session_scope(factory) as session:
        session.add_all(
            CollectionTagNodeRecord(
                digest=f"{index:064x}",
                encoded=f"node-{index}".encode(),
                created_at="2026-09-08T00:00:00.000000Z",
            )
            for index in range(1, 5)
        )

    def reap_one() -> int:
        service = SqlAlchemyCatalogSyncService(
            RuntimeConfig(
                database_url=isolated_database_url,
                catalog_sync_history_reap_batch_size=1,
            ),
            session_factory=make_session_factory(isolated_database_url),
        )
        return service.reap_expired_history()

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(lambda _index: reap_one(), range(2))) == [0, 0]
    with session_scope(factory) as session:
        assert len(list(session.scalars(select(CollectionTagNodeRecord)))) == 2

    restarted = SqlAlchemyCatalogSyncService(
        RuntimeConfig(
            database_url=isolated_database_url,
            catalog_sync_history_reap_batch_size=1,
        ),
        session_factory=make_session_factory(isolated_database_url),
    )
    assert restarted.reap_expired_history() == 0
    assert restarted.reap_expired_history() == 0
    with session_scope(factory) as session:
        assert not list(session.scalars(select(CollectionTagNodeRecord)))


def test_postgres_archive_sequence_state_round_trips_full_v1_domain(
    isolated_database_url: str,
) -> None:
    initialize_db(isolated_database_url)
    access = frozenset({ApplicationAccess(COLLECTIONS_CREATE, ALL_RESOURCES)})
    service = SqlAlchemyCollectionUploadService(
        RuntimeConfig(database_url=isolated_database_url),
        ArchiveStoreRegistry({"archive": archive_store_binding(MemoryArchiveStore())}),
    )
    created = service.create_or_resume(
        idempotency_key="archive-sequence-persistence",
        ingest_source="postgres-fixture",
        archive_store=None,
        initiator=ApplicationPrincipal(app="fixture", key_id="fixture-key", access=access),
        event_context=None,
    )
    collection_id = int(created["collection_id"])
    values = (1 << 63, (1 << 256) - 1)
    with session_scope(make_session_factory(isolated_database_url)) as session:
        upload = session.get(CollectionUploadRecord, collection_id)
        assert upload is not None
        upload.archive_volume_next_sequence = values[-1]
        upload.provenance_archive_next_sequence = values[-1]
        for index, sequence in enumerate(values):
            session.add(
                CollectionArchiveObjectUploadRecord(
                    collection_id=collection_id,
                    object_id=f"pack-{sequence:064x}",
                    sequence=sequence,
                    kind="pack",
                    relative_path=f"volumes/pack-{sequence:064x}.tar.age",
                    object_path=f"archives/fixture/volumes/pack-{sequence:064x}.tar.age",
                    plaintext_bytes=0,
                    source_bytes=0,
                    source_path=None,
                    source_first_part=None,
                    source_part_count=None,
                    unit_plaintext_bytes=1,
                    plan_json="{}",
                    plan_sha256=f"{index + 1:064x}",
                    state="planned",
                    checkpoint_json=None,
                    sealed_receipt_json=None,
                    metadata_receipt_json=None,
                    failure=None,
                    uploaded_bytes=0,
                    uploaded_units=0,
                    total_units=0,
                    updated_at="2026-01-01T00:00:00.000000Z",
                    sealed_at=None,
                )
            )
            session.add(
                CollectionUploadProvenanceArchiveVolumeRecord(
                    collection_id=collection_id,
                    sequence=sequence,
                    kind="bindings",
                    document_json="{}",
                    payload_receipt_json="{}",
                    metadata_receipt_json="{}",
                )
            )

    with session_scope(make_session_factory(isolated_database_url)) as session:
        upload = session.get(CollectionUploadRecord, collection_id)
        assert upload is not None
        assert upload.archive_volume_next_sequence == values[-1]
        assert upload.provenance_archive_next_sequence == values[-1]
        assert list(
            session.scalars(
                select(CollectionArchiveObjectUploadRecord.sequence)
                .where(CollectionArchiveObjectUploadRecord.collection_id == collection_id)
                .order_by(CollectionArchiveObjectUploadRecord.sequence)
            )
        ) == list(values)

    engine = create_catalog_engine(isolated_database_url)
    with engine.begin() as connection:
        assert connection.execute(
            text(
                "SELECT archive_volume_next_sequence, provenance_archive_next_sequence "
                "FROM collection_uploads WHERE collection_id = :collection_id"
            ),
            {"collection_id": collection_id},
        ).one() == (f"{values[-1]:064x}", f"{values[-1]:064x}")
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE collection_uploads SET archive_volume_next_sequence = :sequence "
                    "WHERE collection_id = :collection_id"
                ),
                {"sequence": "g" * 64, "collection_id": collection_id},
            )
    engine.dispose()

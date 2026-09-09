from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
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
    CollectionArchiveCopyRecord,
    CollectionArchiveObjectUploadRecord,
    CollectionDescriptionPublicationRecord,
    CollectionMutableDocumentPublicationAttemptRecord,
    CollectionMutableDocumentReclamationRecord,
    CollectionRecord,
    CollectionTagMutationRecord,
    CollectionTagNodeGcRecord,
    CollectionTagNodeReclamationRecord,
    CollectionTagNodeRecord,
    CollectionTagPublicationFrontierRecord,
    CollectionTagPublicationRecord,
    CollectionTagRevisionRecord,
    CollectionUploadProvenanceArchiveVolumeRecord,
    CollectionUploadRecord,
    RetrievalPlanObjectRecord,
    RetrievalPlanPlacementRecord,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services import collection_tags as collection_tags_module
from riverhog_core.services.catalog_sync import (
    SqlAlchemyCatalogSyncService,
    _reap_unreferenced_tag_history,
)
from riverhog_core.services.collection_descriptions import (
    SqlAlchemyCollectionDescriptionService,
)
from riverhog_core.services.collection_tags import SqlAlchemyCollectionTagService
from riverhog_core.services.collection_uploads import SqlAlchemyCollectionUploadService
from riverhog_core.services.retrieval import SqlAlchemyRetrievalService
from riverhog_protocol import collection_description_identity
from riverhog_protocol.errors import ServiceUnavailable
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from tests.unit.archive_object_fixtures import (
    MemoryArchiveStore,
    archive_store_binding,
)
from tests.unit.test_collection_descriptions import PRINCIPAL as description_principal
from tests.unit.test_collection_descriptions import (
    VersionedDescriptionStore,
)
from tests.unit.test_collection_descriptions import (
    _seed as description_seed,
)
from tests.unit.test_collection_tags import _AmbiguousTagDeleteStore
from tests.unit.test_collection_tags import _principal as tag_principal
from tests.unit.test_collection_tags import _service as tag_service
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
    digests = tuple(f"{index:064x}" for index in range(1, 5))
    with session_scope(factory) as session:
        session.add_all(
            CollectionTagNodeRecord(
                digest=digest,
                encoded=f"node-{index}".encode(),
                created_at="2026-09-08T00:00:00.000000Z",
            )
            for index, digest in enumerate(digests, start=1)
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

    for expected_progress in range(2, 13, 2):
        with ThreadPoolExecutor(max_workers=2) as executor:
            assert list(executor.map(lambda _index: reap_one(), range(2))) == [0, 0]
        with session_scope(factory) as session:
            nodes = set(session.scalars(select(CollectionTagNodeRecord.digest)))
            claims = set(session.scalars(select(CollectionTagNodeReclamationRecord.node_digest)))
            progress = sum(
                (
                    1
                    if digest in nodes and digest in claims
                    else 2
                    if digest not in nodes and digest in claims
                    else 3
                    if digest not in nodes and digest not in claims
                    else 0
                )
                for digest in digests
            )
            assert progress == expected_progress
    assert not nodes
    assert not claims


def test_postgres_tag_mutation_protects_an_aba_root_before_its_first_commit(
    isolated_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, factory, _store = tag_service(None, database_url=isolated_database_url)
    principal = tag_principal("source:camera", "workflow:archive")
    with session_scope(factory) as session:
        collection = session.get(CollectionRecord, 1)
        assert collection is not None
        initial_identity = collection.tag_set_identity
        initial_root = collection.tag_root_sha256
    added = service.add(
        1,
        tag="workflow:archive",
        operation_id="establish-postgres-aba-root",
        expected_revision=1,
        expected_tag_set_identity=initial_identity,
        principal=principal,
    )
    with session_scope(factory) as session:
        retired = session.get(CollectionTagRevisionRecord, (1, 1))
        assert retired is not None and retired.root_sha256 == initial_root
        retired.cleanup_started_at = "2026-01-01T00:00:00.000000Z"

    entered = threading.Event()
    release = threading.Event()
    original_scope = collection_tags_module.session_scope
    paused = False

    @contextmanager
    def pause_before_commit(session_factory: object) -> Iterator[object]:
        nonlocal paused
        with original_scope(session_factory) as session:  # type: ignore[arg-type]
            yield session
            mutation = session.get(
                CollectionTagMutationRecord,
                (1, "reuse-postgres-aba-root"),
            )
            if not paused and mutation is not None and mutation.state == "pending":
                paused = True
                entered.set()
                assert release.wait(timeout=10)

    monkeypatch.setattr(collection_tags_module, "session_scope", pause_before_commit)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(
            service.remove,
            1,
            tag="workflow:archive",
            operation_id="reuse-postgres-aba-root",
            expected_revision=2,
            expected_tag_set_identity=str(added["tag_set_identity"]),
            principal=principal,
        )
        assert entered.wait(timeout=10)
        with session_scope(factory) as session:
            metrics = _reap_unreferenced_tag_history(
                session,
                limit=100,
                cleanup_before="2026-02-01T00:00:00.000000Z",
                cleanup_started_at="2026-02-01T00:00:00.000000Z",
            )
            assert metrics.changed_rows > 0
        with session_scope(factory) as session:
            assert session.get(CollectionTagNodeRecord, initial_root) is not None
            assert session.get(CollectionTagNodeReclamationRecord, initial_root) is None
        release.set()
        result = pending.result(timeout=10)

    assert result["revision"] == 3
    page = service.list_collection(
        1,
        page_size=100,
        position=None,
        expected_revision=3,
        expected_tag_set_identity=str(result["tag_set_identity"]),
        principal=principal,
    )
    assert page["tags"] == ["source:camera"]


def test_postgres_superseded_document_cleanup_workers_claim_distinct_receipts(
    isolated_database_url: str,
) -> None:
    config, factory, _initial, _registry = description_seed(
        None,
        database_url=isolated_database_url,
    )
    store = VersionedDescriptionStore()
    stores = ArchiveStoreRegistry({"archive": archive_store_binding(store)})
    service = SqlAlchemyCollectionDescriptionService(
        config,
        stores,
        session_factory=factory,
    )
    identity = collection_description_identity(
        archive_root_sha256="5" * 64,
        revision=0,
        description=None,
    )
    for description in ("first", "second", "third"):
        result = service.replace(
            1,
            description=description,
            expected_identity=identity,
            principal=description_principal,
        )
        identity = str(result["description_identity"])

    with session_scope(factory) as session:
        assert session.query(CollectionMutableDocumentReclamationRecord).count() == 2
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(lambda _index: service.process_due(limit=1), range(2))) == [
            1,
            1,
        ]
    with session_scope(factory) as session:
        assert session.query(CollectionMutableDocumentReclamationRecord).count() == 0
    assert not store.retained_revisions


def test_postgres_mutable_replica_attempt_serializes_reconciliation_before_newer_desired(
    isolated_database_url: str,
) -> None:
    config, factory, primary, _registry = description_seed(
        None,
        database_url=isolated_database_url,
    )
    initial_identity = collection_description_identity(
        archive_root_sha256="5" * 64,
        revision=0,
        description=None,
    )
    with session_scope(factory) as session:
        session.add(
            CollectionArchiveCopyRecord(
                collection_id=1,
                store="mirror",
                state="uploaded",
                archive_storage_prefix="archives/mirror/1",
                last_uploaded_at="2026-09-07T00:00:00.000000Z",
                last_verified_at="2026-09-07T00:00:00.000000Z",
            )
        )
        session.add(
            CollectionDescriptionPublicationRecord(
                collection_id=1,
                store="mirror",
                desired_revision=0,
                desired_identity=initial_identity,
                published_revision=0,
                published_identity=initial_identity,
                state="published",
                next_attempt_at=None,
            )
        )
    mirror = VersionedDescriptionStore()
    stores = ArchiveStoreRegistry(
        {
            "archive": archive_store_binding(primary),
            "mirror": archive_store_binding(mirror),
        }
    )
    service = SqlAlchemyCollectionDescriptionService(
        config,
        stores,
        session_factory=factory,
    )
    first = service.replace(
        1,
        description="first",
        expected_identity=initial_identity,
        principal=description_principal,
    )
    assert service.process_due(limit=1) == 1
    second = service.replace(
        1,
        description="second",
        expected_identity=str(first["description_identity"]),
        principal=description_principal,
    )
    mirror.lose_next_response = True
    assert service.process_due(limit=1) == 1
    newest = service.replace(
        1,
        description="newest",
        expected_identity=str(second["description_identity"]),
        principal=description_principal,
    )
    with session_scope(factory) as session:
        attempt = session.get(
            CollectionMutableDocumentPublicationAttemptRecord,
            (1, "mirror", "description"),
        )
        publication = session.get(CollectionDescriptionPublicationRecord, (1, "mirror"))
        assert attempt is not None and attempt.document_revision == 2
        assert publication is not None and publication.desired_revision == 3
        publication.next_attempt_at = "2026-09-07T00:00:00.000000Z"

    def advance() -> int:
        worker = SqlAlchemyCollectionDescriptionService(
            config,
            stores,
            session_factory=make_session_factory(isolated_database_url),
        )
        return worker.process_due(limit=1)

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert all(result in {0, 1} for result in executor.map(lambda _index: advance(), range(2)))
    for _ in range(16):
        if service.process_due(limit=1) == 0:
            break
    with session_scope(factory) as session:
        publication = session.get(CollectionDescriptionPublicationRecord, (1, "mirror"))
        assert publication is not None and publication.state == "published"
        assert publication.published_revision == newest["description_revision"] == 3
        assert session.query(CollectionMutableDocumentPublicationAttemptRecord).count() == 0
        assert session.query(CollectionMutableDocumentReclamationRecord).count() == 0
    assert not mirror.retained_revisions


def test_postgres_reused_tag_node_gc_and_publication_workers_converge(
    isolated_database_url: str,
) -> None:
    store = _AmbiguousTagDeleteStore()
    service, factory, _stored = tag_service(
        None,
        archive_store=store,
        database_url=isolated_database_url,
    )
    principal = tag_principal("source:camera", "workflow:archive")
    with session_scope(factory) as session:
        collection = session.get(CollectionRecord, 1)
        assert collection is not None
        initial_identity = collection.tag_set_identity
    added = service.add(
        1,
        tag="workflow:archive",
        operation_id="postgres-gc-add",
        expected_revision=1,
        expected_tag_set_identity=initial_identity,
        principal=principal,
    )
    service.remove(
        1,
        tag="workflow:archive",
        operation_id="postgres-gc-remove",
        expected_revision=2,
        expected_tag_set_identity=str(added["tag_set_identity"]),
        principal=principal,
    )
    with session_scope(factory) as session:
        publication = session.get(CollectionTagPublicationRecord, (1, "archive"))
        assert publication is not None and publication.published_head_identity is not None
        session.query(CollectionTagPublicationFrontierRecord).filter(
            CollectionTagPublicationFrontierRecord.collection_id == 1,
            CollectionTagPublicationFrontierRecord.store == "archive",
            CollectionTagPublicationFrontierRecord.head_identity
            != publication.published_head_identity,
        ).delete(synchronize_session=False)
    for _ in range(128):
        assert service.process_due(limit=1) in {0, 1}
        with session_scope(factory) as session:
            gc = session.scalar(select(CollectionTagNodeGcRecord))
            if gc is not None and gc.state == "retry_wait":
                digest = gc.node_digest
                break
    else:  # pragma: no cover - fixed-depth tag closure is much smaller
        raise AssertionError("tag-node reclamation did not reach its ambiguous result")

    with pytest.raises(ServiceUnavailable):
        service.add(
            1,
            tag="workflow:archive",
            operation_id="postgres-gc-readd",
            expected_revision=3,
            expected_tag_set_identity=initial_identity,
            principal=principal,
        )
    with session_scope(factory) as session:
        gc = session.get(CollectionTagNodeGcRecord, (1, "archive", digest))
        publication = session.get(CollectionTagPublicationRecord, (1, "archive"))
        assert gc is not None and publication is not None
        gc.next_attempt_at = "2026-09-07T00:00:00.000000Z"
        publication.next_attempt_at = "2026-09-07T00:00:00.000000Z"

    def advance() -> int:
        worker = SqlAlchemyCollectionTagService(
            RuntimeConfig(database_url=isolated_database_url),
            ArchiveStoreRegistry({"archive": archive_store_binding(store)}),
            session_factory=make_session_factory(isolated_database_url),
        )
        return worker.process_due(limit=1)

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert all(result in {0, 1} for result in executor.map(lambda _index: advance(), range(2)))
    for _ in range(128):
        progressed = service.process_due(limit=1)
        with session_scope(factory) as session:
            publication = session.get(CollectionTagPublicationRecord, (1, "archive"))
            if publication is not None and publication.published_revision == 4:
                break
        assert progressed == 1
    else:  # pragma: no cover - fixed-depth tag closure is much smaller
        raise AssertionError("reused tag authority did not converge")
    with session_scope(factory) as session:
        assert session.get(CollectionTagNodeGcRecord, (1, "archive", digest)) is None
        assert session.query(CollectionMutableDocumentPublicationAttemptRecord).count() == 0


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

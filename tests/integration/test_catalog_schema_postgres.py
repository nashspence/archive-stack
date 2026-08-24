from __future__ import annotations

import json
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from riverhog_core.app_permissions import (
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
from riverhog_core.catalog_models import TagRecord
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.app_keys import SqlAlchemyAppKeyService
from riverhog_core.services.collection_uploads import SqlAlchemyCollectionUploadService
from riverhog_core.services.retrieval import SqlAlchemyRetrievalService
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url

from tests.fixtures.crypto import FixtureProofStamper
from tests.unit.archive_object_fixtures import (
    MemoryArchiveStore,
    archive_store_binding,
)

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
    url = make_url(value).update_query_dict({"options": f"-csearch_path={schema}"})
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
    assert upgraded.current_revision == validated.current_revision == "v1_0007"
    assert after == before
    engine.dispose()


def test_postgres_v1_fixture_reaches_head_with_archive_identity_leases_and_authorization(
    isolated_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_catalog_engine(isolated_database_url)
    fixture_sql = V1_FIXTURE.read_text(encoding="utf-8")
    with engine.begin() as connection:
        for statement in fixture_sql.split(";\n"):
            if statement.strip():
                connection.exec_driver_sql(statement)
        connection.execute(
            text(
                "INSERT INTO collection_metadata_publications ("
                "collection_id, store, desired_revision, published_revision, state, "
                "attempt_count, next_attempt_at, last_attempt_at, failure, object_path, "
                "version_id, stored_bytes, stored_sha256, published_at"
                ") VALUES ("
                "1, 'fixture-archive', 1, 1, 'published', 1, "
                "'2026-01-02T00:00:00.000000Z', '2026-01-01T00:00:00.000000Z', "
                "'stale failure', 'archives/fixture-archive-id/collection.json', "
                "'fixture-metadata-version', 128, :stored_sha256, "
                "'2026-01-01T00:00:00.000000Z'"
                ")"
            ),
            {"stored_sha256": "d" * 64},
        )

    monkeypatch.setenv("RIVERHOG_STATE_UPGRADE_PASSPHRASE_ID", "fixture-archive-key-v1")
    status = catalog_state_schema(isolated_database_url).upgrade()
    principal = SqlAlchemyAppKeyService(
        RuntimeConfig(database_url=isolated_database_url)
    ).authenticate("fixture-v1-riverhog-token")
    with engine.connect() as connection:
        archive_identity = connection.execute(
            text(
                "SELECT object_path, kind, age_state_json, part_receipts_json, plan_sha256 "
                "FROM collection_archive_objects WHERE collection_id = 1"
            )
        ).one()
        lease = connection.execute(
            text(
                "SELECT owner, source_store, object_id, expires_at "
                "FROM retrieval_cache_leases WHERE collection_id = 1"
            )
        ).one()
        lifecycle_event = connection.execute(
            text("SELECT event_id, owner_app, subject FROM lifecycle_events WHERE sequence = 1")
        ).one()
        existing_provenance = connection.execute(
            text(
                "SELECT encryption_format, passphrase_id, provenance_mode, "
                "provenance_identity FROM collections WHERE id = 1"
            )
        ).one()
        provenance_projection_counts = connection.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM collection_provenance_journals), "
                "(SELECT count(*) FROM collection_file_provenance), "
                "(SELECT count(*) FROM collection_provenance_entities)"
            )
        ).one()
        migrated_retrieval = connection.execute(
            text(
                "SELECT plan_etag, constraints_json FROM retrieval_jobs "
                "WHERE id = 'fixture-retrieval'"
            )
        ).one()
        metadata_publication = connection.execute(
            text(
                "SELECT desired_revision, published_revision, state, attempt_count, "
                "next_attempt_at, last_attempt_at, failure, object_path, version_id, "
                "stored_bytes, stored_sha256, published_at "
                "FROM collection_metadata_publications "
                "WHERE collection_id = 1 AND store = 'fixture-archive'"
            )
        ).one()

    archive_store = MemoryArchiveStore()
    manifest, record_etag = SqlAlchemyRetrievalService(
        RuntimeConfig(database_url=isolated_database_url),
        ArchiveStoreRegistry({"fixture-archive": archive_store_binding(archive_store)}),
        None,
    ).collection_manifest(1, principal=principal)

    assert status.condition == "current"
    assert principal is not None
    assert principal.app == "fixture-client"
    assert principal.allows(CATALOG_READ)
    assert archive_identity.object_path == (
        "archives/fixture-archive-id/volumes/segment-000000000000.age"
    )
    assert archive_identity.kind == "segment"
    assert json.loads(archive_identity.age_state_json)["format"] == "age-v1-scrypt-resumable"
    assert len(json.loads(archive_identity.part_receipts_json)) == 1
    assert len(archive_identity.plan_sha256) == 64
    assert tuple(lease) == (
        "fixture-job",
        "fixture-archive",
        "segment-000000000000",
        "2026-02-01T00:00:00.000000Z",
    )
    assert tuple(lifecycle_event) == ("riverhog-v1-event", "fixture-client", "1")
    assert tuple(existing_provenance) == (
        "age-v1-scrypt",
        "fixture-archive-key-v1",
        "omitted",
        None,
    )
    assert tuple(provenance_projection_counts) == (0, 0, 0)
    migrated_plan = json.loads(migrated_retrieval.constraints_json)
    assert migrated_plan["restore_policy"] == "allow"
    assert migrated_plan["requires_restore"] is True
    assert migrated_retrieval.plan_etag == migrated_plan["etag"]
    assert tuple(metadata_publication) == (
        1,
        None,
        "pending",
        0,
        "2026-01-01T00:00:00.000000Z",
        "2026-01-01T00:00:00.000000Z",
        None,
        "archives/fixture-archive-id/collection.json",
        "fixture-metadata-version",
        128,
        "d" * 64,
        "2026-01-01T00:00:00.000000Z",
    )
    assert manifest.to_mapping() == {
        "format": "riverhog-collection/v1",
        "collection": 1,
        "content_identity": "a" * 64,
        "encryption_format": "age-v1-scrypt",
        "passphrase_id": "fixture-archive-key-v1",
        "provenance_mode": "omitted",
        "provenance_identity": None,
        "metadata_revision": 1,
        "tags": [],
        "files": [
            {
                "path": "notes/fixture.txt",
                "bytes": 12,
                "sha256": "5cb72f90e968922d30557d0af8f719d21f61792becaa87eb32477767d739dc0b",
            }
        ],
    }
    assert len(record_etag) == 64
    engine.dispose()


def test_postgres_v1_fixture_requires_explicit_pre_v1_encryption_identity(
    isolated_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_catalog_engine(isolated_database_url)
    fixture_sql = V1_FIXTURE.read_text(encoding="utf-8")
    with engine.begin() as connection:
        for statement in fixture_sql.split(";\n"):
            if statement.strip():
                connection.exec_driver_sql(statement)
    monkeypatch.delenv("RIVERHOG_STATE_UPGRADE_PASSPHRASE_ID", raising=False)

    with pytest.raises(
        RuntimeError,
        match="RIVERHOG_STATE_UPGRADE_PASSPHRASE_ID is required",
    ):
        catalog_state_schema(isolated_database_url).upgrade()

    engine.dispose()


def test_postgres_upload_idempotency_is_independent_per_application(
    isolated_database_url: str,
) -> None:
    initialize_db(isolated_database_url)
    with session_scope(make_session_factory(isolated_database_url)) as session:
        session.add(
            TagRecord(
                id="photos",
                created_by_app="fixture",
                created_at="2026-01-01T00:00:00.000000Z",
            )
        )
    access = frozenset({ApplicationAccess(COLLECTIONS_CREATE, "tag:photos")})
    memory_store = MemoryArchiveStore()
    archive_stores = ArchiveStoreRegistry({"archive": archive_store_binding(memory_store)})

    def create(app: str, key_id: str) -> dict[str, object]:
        service = SqlAlchemyCollectionUploadService(
            RuntimeConfig(database_url=isolated_database_url),
            archive_stores,
            proof_stamper=FixtureProofStamper(),
        )
        return service.create_or_resume(
            idempotency_key="shared-retry-key",
            tags=("photos",),
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
    upload_tag_indexes = {
        str(index["name"]): index for index in inspect(engine).get_indexes("collection_upload_tags")
    }
    assert upload_tag_indexes["ix_collection_upload_tags_tag"]["column_names"] == [
        "tag_id",
        "collection_id",
    ]
    assert {
        tuple(str(column) for column in constraint["constrained_columns"])
        for constraint in inspect(engine).get_foreign_keys("collection_upload_tags")
    } == {("collection_id",), ("tag_id",)}
    engine.dispose()

from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from riverhog_core.app_permissions import (
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
from riverhog_core.services.collection_uploads import SqlAlchemyCollectionUploadService
from riverhog_protocol.paths import tag_set_identity
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
                "INSERT INTO tags (id, created_by_app, created_at) "
                "VALUES ('fixture', 'fixture', '2026-01-01T00:00:00.000000Z')"
            )
        )
    restarted.dispose()

    assert status.condition == "current"
    assert status.current_revision == "v1_0001"
    assert validate_db(isolated_database_url).condition == "current"


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
            initial_tag="photos",
            tag_set_identity_sha256=tag_set_identity(("photos",)),
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

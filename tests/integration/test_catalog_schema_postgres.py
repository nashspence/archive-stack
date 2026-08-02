from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NoReturn
from uuid import uuid4

import pytest
from riverhog_core.app_permissions import (
    CATALOG_READ,
    COLLECTIONS_CREATE,
    ApplicationAccess,
    ApplicationPrincipal,
)
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
from riverhog_core.services.collections import SqlAlchemyCollectionService
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration
V1_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/state/v1_0001/riverhog.postgresql.sql"


class _UnusedUploadStore:
    def _unexpected(self) -> NoReturn:
        raise AssertionError("upload storage is not used while creating an empty session")

    def create_upload(self, target_path: str, length: int) -> str:
        del target_path, length
        self._unexpected()

    def get_offset(self, tus_url: str) -> int:
        del tus_url
        self._unexpected()

    def append_upload_chunk(
        self,
        tus_url: str,
        *,
        offset: int,
        checksum: str,
        content: bytes,
    ) -> tuple[int, str | None]:
        del tus_url, offset, checksum, content
        self._unexpected()

    def read_target(self, target_path: str) -> bytes:
        del target_path
        self._unexpected()

    def iter_target(
        self,
        target_path: str,
        *,
        offset: int = 0,
        size: int | None = None,
    ) -> Iterator[bytes]:
        del target_path, offset, size
        self._unexpected()
        yield b""

    def delete_target(self, target_path: str) -> None:
        del target_path
        self._unexpected()

    def cancel_upload(self, tus_url: str) -> None:
        del tus_url
        self._unexpected()


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
    assert upgraded.current_revision == validated.current_revision == "v1_0001"
    assert after == before
    engine.dispose()


def test_postgres_v1_fixture_reaches_head_with_archive_identity_leases_and_authorization(
    isolated_database_url: str,
) -> None:
    engine = create_catalog_engine(isolated_database_url)
    fixture_sql = V1_FIXTURE.read_text(encoding="utf-8")
    with engine.begin() as connection:
        for statement in fixture_sql.split(";\n"):
            if statement.strip():
                connection.exec_driver_sql(statement)

    status = catalog_state_schema(isolated_database_url).upgrade()
    principal = SqlAlchemyAppKeyService(
        RuntimeConfig(database_url=isolated_database_url)
    ).authenticate("fixture-v1-riverhog-token")
    with engine.connect() as connection:
        archive_identity = connection.execute(
            text(
                "SELECT object_path, sha256, stored_sha256 "
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

    assert status.condition == "current"
    assert principal is not None
    assert principal.app == "fixture-client"
    assert principal.allows(CATALOG_READ)
    assert tuple(archive_identity) == (
        "collections/1/data-000000.age",
        "d" * 64,
        "e" * 64,
    )
    assert tuple(lease) == (
        "fixture-job",
        "fixture-archive",
        "data-000000",
        "2026-02-01T00:00:00.000000Z",
    )
    assert tuple(lifecycle_event) == ("riverhog-v1-event", "fixture-client", "1")
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

    def create(app: str, key_id: str) -> dict[str, object]:
        service = SqlAlchemyCollectionService(
            RuntimeConfig(database_url=isolated_database_url),
            _UnusedUploadStore(),
        )
        return service.create_or_resume_upload_session(
            idempotency_key="shared-retry-key",
            tags=["photos"],
            initiator=ApplicationPrincipal(app=app, key_id=key_id, access=access),
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

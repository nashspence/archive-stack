from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from riverhog_core.app_permissions import (
    CATALOG_READ,
    COLLECTION_TAGS_MANAGE,
    ApplicationAccess,
    ApplicationPrincipal,
)
from riverhog_core.catalog_db import (
    create_catalog_engine,
    initialize_db,
    make_session_factory,
    session_scope,
)
from riverhog_core.catalog_models import CollectionFileRecord, CollectionRecord, TagRecord
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.tags import SqlAlchemyTagService
from sqlalchemy import text
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration
READER = ApplicationPrincipal(
    app="complete-enumeration-reader",
    key_id=None,
    access=frozenset({ApplicationAccess(CATALOG_READ)}),
)
TAG_MANAGER = ApplicationPrincipal(
    app="tag-manager",
    key_id=None,
    access=frozenset({ApplicationAccess(COLLECTION_TAGS_MANAGE)}),
)


@pytest.fixture
def isolated_database_url() -> Iterator[str]:
    value = os.getenv("RIVERHOG_TEST_POSTGRES_URL", "").strip()
    if not value:
        pytest.skip("RIVERHOG_TEST_POSTGRES_URL is required")
    schema = f"riverhog_enumeration_{uuid4().hex}"
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


def test_complete_enumeration_keeps_one_snapshot_while_writes_continue(
    isolated_database_url: str,
) -> None:
    initialize_db(isolated_database_url)
    sessions = make_session_factory(isolated_database_url)
    with session_scope(sessions) as session:
        session.add_all(
            TagRecord(
                id=f"tag-{index:04}",
                created_by_app="fixture",
                created_at="2026-08-28T00:00:00.000000Z",
            )
            for index in range(250)
        )
    service = SqlAlchemyTagService(
        RuntimeConfig(database_url=isolated_database_url),
        session_factory=sessions,
    )
    stream = service.iter_tags(q=None, sort="id", order="asc", principal=READER)

    first = next(stream)
    with ThreadPoolExecutor(max_workers=1) as executor:
        inserted = executor.submit(
            service.create,
            "tag-after-snapshot",
            creator=READER,
        ).result(timeout=5)
    snapshot = [first, *stream]

    assert inserted["id"] == "tag-after-snapshot"
    assert [item["id"] for item in snapshot] == [f"tag-{index:04}" for index in range(250)]
    assert [
        item["id"] for item in service.iter_tags(q=None, sort="id", order="asc", principal=READER)
    ] == [*(f"tag-{index:04}" for index in range(250)), "tag-after-snapshot"]


def test_canceling_complete_enumeration_releases_its_snapshot(
    isolated_database_url: str,
) -> None:
    initialize_db(isolated_database_url)
    sessions = make_session_factory(isolated_database_url)
    service = SqlAlchemyTagService(
        RuntimeConfig(database_url=isolated_database_url),
        session_factory=sessions,
    )
    service.create("tag-before-cancel", creator=READER)
    stream = service.iter_tags(q=None, sort="id", order="asc", principal=READER)

    assert next(stream)["id"] == "tag-before-cancel"
    stream.close()
    with ThreadPoolExecutor(max_workers=1) as executor:
        inserted = executor.submit(
            service.create,
            "tag-after-cancel",
            creator=READER,
        ).result(timeout=5)

    assert inserted["id"] == "tag-after-cancel"


def test_concurrent_collection_mutations_keep_tag_count_projection_exact(
    isolated_database_url: str,
) -> None:
    initialize_db(isolated_database_url)
    sessions = make_session_factory(isolated_database_url)
    now = "2026-08-28T00:00:00.000000Z"
    with session_scope(sessions) as session:
        session.add(TagRecord(id="shared", created_by_app="fixture", created_at=now))
        session.add_all(
            CollectionRecord(
                id=collection_id,
                creation_idempotency_key=f"fixture-{collection_id}",
                creation_identity_sha256=f"{collection_id}" * 64,
                creation_custody_mode="producer-retained",
                content_identity=f"{collection_id}" * 64,
                encryption_format="age-v1-scrypt",
                passphrase_id="fixture-archive-key-v1",
                provenance_mode="omitted",
                provenance_identity=None,
                record_etag=f"{collection_id}" * 64,
                metadata_revision=1,
                metadata_updated_at=now,
                created_by_app="fixture",
                created_at=now,
                file_count=1,
                file_bytes=0,
                files=[
                    CollectionFileRecord(
                        collection_id=collection_id,
                        path=f"fixture-{collection_id}.bin",
                        bytes=0,
                        sha256=f"{collection_id}" * 64,
                    )
                ],
            )
            for collection_id in (1, 2)
        )
    first = SqlAlchemyTagService(
        RuntimeConfig(database_url=isolated_database_url),
        session_factory=sessions,
    )
    second = SqlAlchemyTagService(
        RuntimeConfig(database_url=isolated_database_url),
        session_factory=sessions,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = (
            executor.submit(first.add_collection_tag, 1, "shared", principal=TAG_MANAGER),
            executor.submit(second.add_collection_tag, 2, "shared", principal=TAG_MANAGER),
        )
        assert [future.result(timeout=10)["tags"] for future in results] == [
            ["shared"],
            ["shared"],
        ]

    with session_scope(sessions) as session:
        shared = session.get(TagRecord, "shared")
        assert shared is not None and shared.collection_count == 2

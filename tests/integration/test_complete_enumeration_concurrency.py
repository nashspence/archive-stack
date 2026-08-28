from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from riverhog_core.app_permissions import CATALOG_READ, ApplicationAccess, ApplicationPrincipal
from riverhog_core.catalog_db import (
    create_catalog_engine,
    initialize_db,
    make_session_factory,
    session_scope,
)
from riverhog_core.catalog_models import TagRecord
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


@pytest.fixture
def isolated_database_url() -> Iterator[str]:
    value = os.getenv("RIVERHOG_TEST_POSTGRES_URL", "").strip()
    if not value:
        pytest.skip("RIVERHOG_TEST_POSTGRES_URL is required")
    schema = f"riverhog_enumeration_{uuid4().hex}"
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

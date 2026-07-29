from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
from riverhog_core.catalog_db import create_catalog_engine, initialize_db
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration


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
    initialize_db(isolated_database_url)
    initialize_db(isolated_database_url)
    engine = create_catalog_engine(isolated_database_url)
    with engine.begin() as connection:
        connection.execute(text("DROP INDEX ix_retrieval_jobs_due"))

    with pytest.raises(
        RuntimeError,
        match=r"missing index retrieval_jobs\.ix_retrieval_jobs_due",
    ):
        initialize_db(isolated_database_url)

    assert "ix_retrieval_jobs_due" not in {
        index["name"] for index in inspect(engine).get_indexes("retrieval_jobs")
    }
    engine.dispose()

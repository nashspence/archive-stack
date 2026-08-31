from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from riverhog_core.catalog_db import (
    STATE_VERSION_TABLE,
    Base,
    create_catalog_engine,
    initialize_db,
    make_session_factory,
    session_scope,
)
from riverhog_core.catalog_models import RetrievalCacheStoreAccountingRecord
from riverhog_core.ports.archive_objects import WriteSession
from riverhog_core.runtime_config import (
    RetrievalCacheStoreRegistration,
    StorageAdapterRegistration,
)
from riverhog_core.services.retrieval_cache import SqlAlchemyRetrievalCache
from sqlalchemy import text

pytestmark = pytest.mark.integration


@pytest.fixture
def database_url() -> Iterator[str]:
    value = os.getenv("RIVERHOG_TEST_POSTGRES_URL", "").strip()
    if not value:
        pytest.skip("RIVERHOG_TEST_POSTGRES_URL is required")
    engine = create_catalog_engine(value)
    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.execute(text(f'DROP TABLE IF EXISTS "{STATE_VERSION_TABLE}"'))
    engine.dispose()
    initialize_db(value)
    try:
        yield value
    finally:
        engine = create_catalog_engine(value)
        Base.metadata.drop_all(engine)
        with engine.begin() as connection:
            connection.execute(text(f'DROP TABLE IF EXISTS "{STATE_VERSION_TABLE}"'))
        engine.dispose()


class _Candidate:
    name = "local"

    @staticmethod
    def object_path(source_store: str, collection_id: int, object_id: str) -> str:
        return f"objects/{source_store}/{collection_id}/{object_id}"

    def find_completed_population(self, **_: object) -> None:
        return None

    def begin_population(
        self,
        *,
        source_store: str,
        collection_id: int,
        object_id: str,
        expected_bytes: int,
    ) -> WriteSession:
        return WriteSession(
            self.object_path(source_store, collection_id, object_id),
            f"write-{object_id}",
            expected_bytes,
        )


def _cache(database_url: str) -> SqlAlchemyRetrievalCache:
    registration = RetrievalCacheStoreRegistration(
        name="local",
        adapter=StorageAdapterRegistration(
            name="local",
            base_url="https://local.example.test",
            token_file=Path("/run/secrets/local.token"),
        ),
        admission_budget_bytes=100,
    )
    return SqlAlchemyRetrievalCache(
        {"local": _Candidate()},  # type: ignore[arg-type]
        {"local": registration},
        session_factory=make_session_factory(database_url),
    )


def test_postgres_serializes_exact_cache_admission_across_service_instances(
    database_url: str,
) -> None:
    caches = (_cache(database_url), _cache(database_url))
    barrier = threading.Barrier(2)

    def admit(item: tuple[int, SqlAlchemyRetrievalCache]) -> object | None:
        index, cache = item
        barrier.wait()
        return cache.admit(
            owner=f"worker:{item[0]}",
            source_store="deep",
            collection_id=1,
            object_id=f"volume-{index}",
            expected_bytes=60,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        admissions = list(executor.map(admit, enumerate(caches)))

    assert sum(current is not None for current in admissions) == 1
    with session_scope(make_session_factory(database_url)) as session:
        accounting = session.get(RetrievalCacheStoreAccountingRecord, "local")
        assert accounting is not None
        assert accounting.reserved_bytes == 60
        assert accounting.committed_bytes == 0

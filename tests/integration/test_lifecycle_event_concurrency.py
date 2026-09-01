from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import uuid4

import pytest
from riverhog_core.catalog_db import (
    create_catalog_engine,
    initialize_db,
    make_session_factory,
    session_scope,
)
from riverhog_core.catalog_models import LifecycleEventRecord
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.lifecycle_events import SqlAlchemyLifecycleEventService
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from time_formats import utc_timestamp_now

pytestmark = pytest.mark.integration


@pytest.fixture
def database_url() -> Iterator[str]:
    value = os.environ.get("RIVERHOG_TEST_POSTGRES_URL", "").strip()
    if not value:
        pytest.skip("RIVERHOG_TEST_POSTGRES_URL is required")
    schema = "riverhog_lifecycle_" + uuid4().hex
    admin = create_catalog_engine(value)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped = (
        make_url(value)
        .update_query_dict({"options": f"-csearch_path={schema},public"})
        .render_as_string(hide_password=False)
    )
    initialize_db(scoped)
    try:
        yield scoped
    finally:
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def _race(first: Callable[[], Any], second: Callable[[], Any]) -> tuple[Any, Any]:
    barrier = threading.Barrier(2)

    def invoke(call: Callable[[], Any]) -> Any:
        barrier.wait(timeout=10)
        return call()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (executor.submit(invoke, first), executor.submit(invoke, second))
        return futures[0].result(timeout=20), futures[1].result(timeout=20)


def _event_data(collection_id: int) -> dict[str, object]:
    return {
        "collection_id": collection_id,
        "collection_created_at": utc_timestamp_now(),
        "collection_tag_count": 0,
        "actor": {"app": "riverhog"},
        "initiator": {"app": "fixture"},
    }


def test_event_reads_and_concurrent_context_reapers_do_only_bounded_work(
    database_url: str,
) -> None:
    config = RuntimeConfig(
        database_url=database_url,
        event_context_reap_batch_size=5,
    )
    first = SqlAlchemyLifecycleEventService(config)
    second = SqlAlchemyLifecycleEventService(config)
    for ordinal in range(23):
        first.emit(
            owner_app="fixture",
            type="collection.tags_changed",
            subject=str(ordinal + 1),
            data=_event_data(ordinal + 1),
            context_json='{"route":"fixture"}',
            context_expires_at="2000-01-01T00:00:00.000000Z",
        )
    first.emit(
        owner_app="fixture",
        type="collection.tags_changed",
        subject="24",
        data=_event_data(24),
        context_json='{"route":"future"}',
        context_expires_at="2999-01-01T00:00:00.000000Z",
    )

    page = first.page(owner_app="fixture", after=None, limit=3)
    assert len(page.events) == 3
    assert all("context" not in event.data for event in page.events)
    with session_scope(make_session_factory(database_url)) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(LifecycleEventRecord)
                .where(
                    LifecycleEventRecord.context_json.is_not(None),
                    LifecycleEventRecord.context_expires_at == "2000-01-01T00:00:00.000000Z",
                )
            )
            == 23
        )

    assert sorted(_race(first.reap_expired_contexts, second.reap_expired_contexts)) == [5, 5]
    restarted = SqlAlchemyLifecycleEventService(config)
    assert [restarted.reap_expired_contexts() for _ in range(4)] == [5, 5, 3, 0]
    with session_scope(make_session_factory(database_url)) as session:
        remaining = list(
            session.scalars(
                select(LifecycleEventRecord).where(LifecycleEventRecord.context_json.is_not(None))
            )
        )
        assert [record.subject for record in remaining] == ["24"]

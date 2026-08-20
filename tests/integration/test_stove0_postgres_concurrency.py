from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from stove0_core import (
    ConcurrentWorkUpdate,
    SqlAlchemyStateStore,
    Stove0WorkService,
    stove0_state_schema,
)
from stove0_protocol import CollectionRootRef, RecipeRef, WorkIdentity, WorkPayload

pytestmark = pytest.mark.integration


@pytest.fixture
def stores() -> Iterator[tuple[SqlAlchemyStateStore, SqlAlchemyStateStore]]:
    database_url = os.environ.get("RIVERHOG_TEST_POSTGRES_URL", "").strip()
    if not database_url:
        pytest.skip("RIVERHOG_TEST_POSTGRES_URL is required")
    schema = "stove0_test_" + uuid.uuid4().hex
    bootstrap = create_engine(database_url)
    with bootstrap.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    bootstrap.dispose()
    scoped_url = str(
        make_url(database_url)
        .update_query_dict({"options": f"-csearch_path={schema}"})
        .render_as_string(hide_password=False)
    )
    assert stove0_state_schema(scoped_url).upgrade().condition == "current"
    assert stove0_state_schema(scoped_url).validate().current_revision == "v1_0001"
    first_engine = create_engine(
        scoped_url,
        pool_pre_ping=True,
    )
    second_engine = create_engine(
        scoped_url,
        pool_pre_ping=True,
    )
    first = SqlAlchemyStateStore(scoped_url, engine=first_engine, initialize=False)
    second = SqlAlchemyStateStore(scoped_url, engine=second_engine, initialize=False)
    try:
        yield first, second
    finally:
        first.engine.dispose()
        second.engine.dispose()
        cleanup = create_engine(database_url)
        with cleanup.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        cleanup.dispose()


def _work() -> WorkIdentity:
    return WorkIdentity.seal(
        WorkPayload(
            recipe=RecipeRef(id="camera.archive/v1", revision=1, sha256="a" * 64),
            inputs=(
                CollectionRootRef(
                    collection_id=1,
                    manifest_sha256="b" * 64,
                    content_etag="c" * 64,
                ),
            ),
        )
    )


def test_postgres_concurrent_create_converges_and_controller_worker_cas_is_fenced(
    stores: tuple[SqlAlchemyStateStore, SqlAlchemyStateStore],
) -> None:
    first, second = stores
    work = _work()
    barrier = threading.Barrier(2)
    created: list[object] = []
    failures: list[BaseException] = []

    def create(store: SqlAlchemyStateStore) -> None:
        try:
            barrier.wait(timeout=5)
            created.append(Stove0WorkService(store).create_or_resume(work))
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=create, args=(store,)) for store in stores]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not failures
    assert len(created) == 2 and created[0] == created[1]
    initial = first.load(work.work_id)
    assert initial is not None
    service_a = Stove0WorkService(first)
    service_b = Stove0WorkService(second)
    barrier = threading.Barrier(2)
    winners: list[object] = []
    stale: list[ConcurrentWorkUpdate] = []

    def claim(service: Stove0WorkService) -> None:
        try:
            barrier.wait(timeout=5)
            winners.append(
                service.bind_claim(
                    work.work_id,
                    claim_id=work.work_id,
                    fence=1,
                    expected_revision=initial.revision,
                )
            )
        except ConcurrentWorkUpdate as exc:
            stale.append(exc)

    threads = [
        threading.Thread(target=claim, args=(service,)) for service in (service_a, service_b)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(winners) == 1
    assert len(stale) == 1
    current = first.load(work.work_id)
    assert current is not None and current.phase == "claimed" and current.revision == 2


def test_postgres_event_cursor_compare_and_swap_prevents_replayed_cursor_regression(
    stores: tuple[SqlAlchemyStateStore, SqlAlchemyStateStore],
) -> None:
    first, second = stores
    assert first.compare_and_swap_cursor("riverhog/v1", expected_revision=None, cursor="10") == (
        "10",
        1,
    )
    assert second.load_cursor("riverhog/v1") == ("10", 1)
    with pytest.raises(ConcurrentWorkUpdate, match="revision is stale"):
        second.compare_and_swap_cursor("riverhog/v1", expected_revision=0, cursor="9")
    assert second.compare_and_swap_cursor("riverhog/v1", expected_revision=1, cursor="11") == (
        "11",
        2,
    )

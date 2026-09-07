from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import riverhog_core.services.catalog_sync as catalog_sync_service
from pydantic import ValidationError
from riverhog_api.app import create_app
from riverhog_api_client import CatalogReplica
from riverhog_application_access import ALL_RESOURCES, CATALOG_READ, ApplicationAccess
from riverhog_core.app_permissions import ApplicationPrincipal
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_events import (
    begin_catalog_event,
    publish_catalog_event,
    record_catalog_event,
)
from riverhog_core.catalog_models import (
    CatalogEventRecord,
    CatalogSyncStateRecord,
    CollectionRecord,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.catalog_sync import SqlAlchemyCatalogSyncService
from riverhog_protocol import (
    CATALOG_SYNC_FORMAT,
    MAX_CATALOG_SYNC_REVISION,
    CatalogSyncChangePage,
    CatalogSyncCheckpoint,
    CatalogSyncCollectionPage,
    CatalogSyncDelete,
    CatalogSyncDescriptor,
    CatalogSyncUpsert,
)
from riverhog_protocol.errors import (
    CatalogSyncCursorExpired,
    CatalogSyncHistoryExpired,
    CatalogSyncSourceChanged,
    CatalogSyncViewChanged,
    Forbidden,
)
from sqlalchemy import select

from tests.unit.db_helpers import sqlite_url

NOW = "2026-09-07T00:00:00.000000Z"
PRINCIPAL = ApplicationPrincipal(
    app="indexer",
    key_id="indexer-key",
    access=frozenset({ApplicationAccess(CATALOG_READ, ALL_RESOURCES)}),
    authorization_view_identity="f" * 64,
)


def _service(path: Path) -> tuple[SqlAlchemyCatalogSyncService, object]:
    config = RuntimeConfig(
        database_url=sqlite_url(path),
        browse_token_signing_key="catalog-sync-test-key-000000000000",
    )
    initialize_db(config.database_url)
    return SqlAlchemyCatalogSyncService(config), make_session_factory(config.database_url)


def _seed(factory: object, collection_id: int) -> None:
    with session_scope(factory) as session:  # type: ignore[arg-type]
        root = f"{collection_id:064x}"
        collection = CollectionRecord(
            id=collection_id,
            creation_idempotency_key=f"collection-{collection_id}",
            creation_identity_sha256=root,
            creation_custody_mode="producer-retained",
            archive_generation=root,
            content_identity=root,
            encryption_format="age-v1-scrypt",
            passphrase_id="test-key",
            provenance_mode="omitted",
            provenance_identity=None,
            inventory_identity=root,
            archive_root_sha256=root,
            created_by_app="fixture",
            created_at=NOW,
            is_published=True,
            file_count=0,
            file_bytes=0,
        )
        session.add(collection)
        session.flush()
        record_catalog_event(
            session,
            change="created",
            collection_id=collection_id,
            occurred_at=NOW,
            inventory_identity=root,
            before_groups=(),
            after_groups=(),
        )


def _delete(factory: object, collection_id: int) -> None:
    with session_scope(factory) as session:  # type: ignore[arg-type]
        collection = session.get(CollectionRecord, collection_id)
        assert collection is not None
        event = begin_catalog_event(
            session,
            change="deleted",
            collection_id=collection_id,
            occurred_at=NOW,
            inventory_identity=collection.inventory_identity,
        )
        publish_catalog_event(session, event=event)
        session.delete(collection)


def test_catalog_sync_bootstrap_and_follow_are_exact_bounded_authorities(
    tmp_path: Path,
) -> None:
    service, factory = _service(tmp_path / "catalog.sqlite3")
    for collection_id in (1, 2, 3):
        _seed(factory, collection_id)

    checkpoint = service.checkpoint(principal=PRINCIPAL)
    _seed(factory, 4)

    first = service.collections(
        cursor=checkpoint.catalog_cursor,
        limit=2,
        principal=PRINCIPAL,
    )
    assert [item.collection_id for item in first.collections] == [1, 2]
    assert first.next_cursor is not None
    assert first.changes_cursor is None

    final = service.collections(cursor=first.next_cursor, limit=2, principal=PRINCIPAL)
    assert [item.collection_id for item in final.collections] == [3]
    assert final.next_cursor is None
    assert final.changes_cursor is not None

    catchup = service.changes(cursor=final.changes_cursor, limit=1, principal=PRINCIPAL)
    assert catchup.changes == [
        CatalogSyncUpsert(
            collection_id=4,
            archive_root_sha256=f"{4:064x}",
            content_identity=f"{4:064x}",
            revision="4",
        )
    ]
    assert catchup.caught_up is True

    with session_scope(factory) as session:  # type: ignore[arg-type]
        collection = session.get(CollectionRecord, 2)
        assert collection is not None
        event = begin_catalog_event(
            session,
            change="deleted",
            collection_id=2,
            occurred_at=NOW,
            inventory_identity=collection.inventory_identity,
        )
        publish_catalog_event(session, event=event)
        session.delete(collection)

    followed = service.changes(cursor=catchup.next_cursor, limit=1, principal=PRINCIPAL)
    assert followed.changes == [CatalogSyncDelete(collection_id=2, revision="5")]
    assert followed.caught_up is True


def test_catalog_sync_cursor_fails_closed_for_authority_changes(tmp_path: Path) -> None:
    service, factory = _service(tmp_path / "catalog.sqlite3")
    checkpoint = service.checkpoint(principal=PRINCIPAL)

    changed_view = replace(PRINCIPAL, authorization_view_identity="e" * 64)
    with pytest.raises(CatalogSyncViewChanged):
        service.collections(cursor=checkpoint.catalog_cursor, limit=1, principal=changed_view)

    rotated_credential = replace(PRINCIPAL, key_id="replacement-key")
    with pytest.raises(Forbidden):
        service.collections(
            cursor=checkpoint.catalog_cursor,
            limit=1,
            principal=rotated_credential,
        )

    with session_scope(factory) as session:  # type: ignore[arg-type]
        state = session.get(CatalogSyncStateRecord, 1)
        assert state is not None
        state.source_identity = "d" * 64
    with pytest.raises(CatalogSyncSourceChanged):
        service.collections(cursor=checkpoint.catalog_cursor, limit=1, principal=PRINCIPAL)


def test_catalog_sync_cursor_expiry_is_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 9, 7, tzinfo=UTC)
    monkeypatch.setattr(catalog_sync_service, "utc_now", lambda: now)
    service, _factory = _service(tmp_path / "catalog.sqlite3")
    checkpoint = service.checkpoint(principal=PRINCIPAL)

    monkeypatch.setattr(
        catalog_sync_service,
        "utc_now",
        lambda: now + timedelta(days=8),
    )
    with pytest.raises(CatalogSyncCursorExpired):
        service.collections(cursor=checkpoint.catalog_cursor, limit=1, principal=PRINCIPAL)


def test_catalog_sync_crosses_many_pages_and_repairs_fixed_frontier_changes(
    tmp_path: Path,
) -> None:
    service, factory = _service(tmp_path / "catalog.sqlite3")
    for collection_id in range(1, 206):
        _seed(factory, collection_id)

    checkpoint = service.checkpoint(principal=PRINCIPAL)
    first = service.collections(cursor=checkpoint.catalog_cursor, limit=17, principal=PRINCIPAL)
    assert [item.collection_id for item in first.collections] == list(range(1, 18))
    assert first.next_cursor is not None

    _delete(factory, 7)
    _delete(factory, 150)
    _seed(factory, 206)

    descriptors = list(first.collections)
    cursor = first.next_cursor
    page_count = 1
    while cursor is not None:
        page = service.collections(cursor=cursor, limit=17, principal=PRINCIPAL)
        assert len(page.collections) <= 17
        descriptors.extend(page.collections)
        cursor = page.next_cursor
        page_count += 1
    assert page_count > 10
    assert {item.collection_id for item in descriptors} == set(range(1, 206)) - {150}
    assert page.changes_cursor is not None

    _seed(factory, 207)
    catchup = service.changes(cursor=page.changes_cursor, limit=2, principal=PRINCIPAL)
    assert catchup.caught_up is False
    assert len(catchup.changes) <= 2
    catchup = service.changes(cursor=catchup.next_cursor, limit=2, principal=PRINCIPAL)
    assert catchup.caught_up is True
    assert [item.collection_id for item in catchup.changes] == [206]

    followed = service.changes(cursor=catchup.next_cursor, limit=2, principal=PRINCIPAL)
    assert followed.caught_up is True
    assert [item.collection_id for item in followed.changes] == [207]


def test_catalog_sync_history_reaper_advances_only_a_bounded_expired_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, factory = _service(tmp_path / "catalog.sqlite3")
    for collection_id in (1, 2, 3):
        _seed(factory, collection_id)
    with session_scope(factory) as session:  # type: ignore[arg-type]
        events = list(
            session.scalars(select(CatalogEventRecord).order_by(CatalogEventRecord.revision))
        )
        events[0].committed_at = "2026-01-01T00:00:00.000000Z"
        events[1].committed_at = "2026-01-02T00:00:00.000000Z"
        events[2].committed_at = "2026-09-07T00:00:00.000000Z"
    monkeypatch.setattr(
        catalog_sync_service,
        "utc_now",
        lambda: datetime(2026, 9, 7, tzinfo=UTC),
    )

    assert service.reap_expired_history(limit=1) == 1
    with session_scope(factory) as session:  # type: ignore[arg-type]
        state = session.get(CatalogSyncStateRecord, 1)
        assert state is not None and state.retained_revision == 1
        assert list(session.scalars(select(CatalogEventRecord.revision))) == [2, 3]
    assert service.reap_expired_history(limit=1) == 1
    assert service.reap_expired_history(limit=1) == 0


def test_catalog_sync_history_reaping_has_an_explicit_gap_error(tmp_path: Path) -> None:
    service, factory = _service(tmp_path / "catalog.sqlite3")
    _seed(factory, 1)
    checkpoint = service.checkpoint(principal=PRINCIPAL)
    page = service.collections(cursor=checkpoint.catalog_cursor, limit=1, principal=PRINCIPAL)
    assert page.changes_cursor is not None
    _seed(factory, 2)
    with session_scope(factory) as session:  # type: ignore[arg-type]
        state = session.get(CatalogSyncStateRecord, 1)
        assert state is not None
        state.retained_revision = 2
    with pytest.raises(CatalogSyncHistoryExpired):
        service.changes(cursor=page.changes_cursor, limit=1, principal=PRINCIPAL)


class _ReplicaApi:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def create_catalog_sync_checkpoint(self) -> CatalogSyncCheckpoint:
        self.calls.append("checkpoint")
        return CatalogSyncCheckpoint(
            source_identity="a" * 64,
            authorization_view_identity="b" * 64,
            catalog_cursor="catalog-1",
        )

    def list_catalog_sync_collections(
        self, cursor: str, *, limit: int = 100
    ) -> CatalogSyncCollectionPage:
        self.calls.append(f"catalog:{cursor}:{limit}")
        assert cursor == "catalog-1"
        return CatalogSyncCollectionPage(
            source_identity="a" * 64,
            authorization_view_identity="b" * 64,
            collections=[
                CatalogSyncDescriptor(
                    collection_id=1,
                    archive_root_sha256="c" * 64,
                    content_identity="d" * 64,
                    revision="1",
                )
            ],
            changes_cursor="changes-1",
        )

    def list_catalog_sync_changes(self, cursor: str, *, limit: int = 100) -> CatalogSyncChangePage:
        self.calls.append(f"changes:{cursor}:{limit}")
        assert cursor == "changes-1"
        return CatalogSyncChangePage(
            source_identity="a" * 64,
            authorization_view_identity="b" * 64,
            changes=[],
            next_cursor="changes-2",
            caught_up=True,
            through_revision="1",
        )


def test_catalog_replica_uses_one_request_per_step_and_publishes_atomically(
    tmp_path: Path,
) -> None:
    api = _ReplicaApi()
    replica = CatalogReplica(tmp_path / "replica.sqlite3")

    replica.start(api)
    assert api.calls == ["checkpoint"]
    with pytest.raises(RuntimeError):
        replica.page()

    replica.step(api, limit=1)
    assert api.calls[-1] == "catalog:catalog-1:1"
    with pytest.raises(RuntimeError):
        replica.page()

    replica.step(api, limit=1)
    assert api.calls[-1] == "changes:changes-1:1"
    assert [item.collection_id for item in replica.page()] == [1]

    replica.start(api)
    assert [item.collection_id for item in replica.page()] == [1]


def test_catalog_replica_retries_a_lost_page_without_advancing_local_state(
    tmp_path: Path,
) -> None:
    class LostOnce(_ReplicaApi):
        lost = False

        def list_catalog_sync_collections(
            self, cursor: str, *, limit: int = 100
        ) -> CatalogSyncCollectionPage:
            page = super().list_catalog_sync_collections(cursor, limit=limit)
            if not self.lost:
                self.lost = True
                raise OSError("response lost")
            return page

    api = LostOnce()
    replica = CatalogReplica(tmp_path / "replica.sqlite3")
    before = replica.start(api)

    with pytest.raises(OSError, match="response lost"):
        replica.step(api, limit=1)
    assert replica.status() == before

    restarted = CatalogReplica(tmp_path / "replica.sqlite3")
    assert restarted.step(api, limit=1)["phase"] == "catchup"


def test_catalog_replica_rejects_cross_page_reordering(tmp_path: Path) -> None:
    class ReorderedApi(_ReplicaApi):
        def list_catalog_sync_collections(
            self, cursor: str, *, limit: int = 100
        ) -> CatalogSyncCollectionPage:
            if cursor == "catalog-1":
                return CatalogSyncCollectionPage(
                    source_identity="a" * 64,
                    authorization_view_identity="b" * 64,
                    collections=[
                        CatalogSyncDescriptor(
                            collection_id=2,
                            archive_root_sha256="c" * 64,
                            content_identity="d" * 64,
                            revision="1",
                        )
                    ],
                    next_cursor="catalog-2",
                )
            assert cursor == "catalog-2"
            return CatalogSyncCollectionPage(
                source_identity="a" * 64,
                authorization_view_identity="b" * 64,
                collections=[
                    CatalogSyncDescriptor(
                        collection_id=1,
                        archive_root_sha256="e" * 64,
                        content_identity="f" * 64,
                        revision="2",
                    )
                ],
                changes_cursor="changes-1",
            )

    api = ReorderedApi()
    replica = CatalogReplica(tmp_path / "replica.sqlite3")
    replica.start(api)
    replica.step(api, limit=1)
    before = replica.status()

    with pytest.raises(ValueError, match="not canonical"):
        replica.step(api, limit=1)
    assert replica.status() == before


def test_catalog_replica_reclaims_settled_tombstones_in_bounded_steps(
    tmp_path: Path,
) -> None:
    class DeletedAfterCatchup(_ReplicaApi):
        def list_catalog_sync_changes(
            self, cursor: str, *, limit: int = 100
        ) -> CatalogSyncChangePage:
            if cursor == "changes-1":
                return super().list_catalog_sync_changes(cursor, limit=limit)
            assert cursor == "changes-2"
            return CatalogSyncChangePage(
                source_identity="a" * 64,
                authorization_view_identity="b" * 64,
                changes=[CatalogSyncDelete(collection_id=1, revision="2")],
                next_cursor="changes-3",
                caught_up=True,
                through_revision="2",
            )

    api = DeletedAfterCatchup()
    replica = CatalogReplica(tmp_path / "replica.sqlite3")
    replica.start(api)
    replica.step(api, limit=1)
    replica.step(api, limit=1)
    replica.step(api, limit=1)
    assert replica.get(1) is None

    assert replica.reclaim(limit=1) == 1
    assert replica.reclaim(limit=1) == 0
    assert replica.get(1) is None


def test_competing_catalog_replica_workers_commit_only_one_page(
    tmp_path: Path,
) -> None:
    barrier = threading.Barrier(2)

    class ConcurrentApi(_ReplicaApi):
        def list_catalog_sync_collections(
            self, cursor: str, *, limit: int = 100
        ) -> CatalogSyncCollectionPage:
            page = super().list_catalog_sync_collections(cursor, limit=limit)
            barrier.wait(timeout=5)
            return page

    api = ConcurrentApi()
    database = tmp_path / "replica.sqlite3"
    CatalogReplica(database).start(api)

    def step() -> str:
        return str(CatalogReplica(database).step(api, limit=1)["phase"])

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [executor.submit(step) for _ in range(2)]
        outcomes: list[str] = []
        failures: list[RuntimeError] = []
        for result in results:
            try:
                outcomes.append(result.result(timeout=5))
            except RuntimeError as exc:  # one stale worker must lose the serial fence
                failures.append(exc)

    assert outcomes == ["catchup"]
    assert len(failures) == 1
    assert isinstance(failures[0], RuntimeError)
    assert CatalogReplica(database).status()["phase"] == "catchup"


def test_native_catalog_sync_has_three_bounded_operations() -> None:
    paths = create_app().openapi()["paths"]
    assert {
        "/v1/catalog-sync/checkpoint",
        "/v1/catalog-sync/collections",
        "/v1/catalog-sync/changes",
    }.issubset(paths)
    assert (
        paths["/v1/catalog-sync/collections"]["get"]["x-riverhog-read-collection"]["kind"]
        == "exact-authority-page"
    )
    assert (
        paths["/v1/catalog-sync/changes"]["get"]["x-riverhog-read-collection"]["kind"]
        == "cursor-feed"
    )
    assert CatalogSyncCheckpoint.model_fields["format"].default == CATALOG_SYNC_FORMAT


def test_catalog_sync_documents_fail_closed_on_ambiguous_continuations() -> None:
    envelope = {
        "source_identity": "a" * 64,
        "authorization_view_identity": "b" * 64,
        "collections": [],
    }
    with pytest.raises(ValidationError):
        CatalogSyncCollectionPage(**envelope)
    with pytest.raises(ValidationError):
        CatalogSyncCollectionPage(
            **envelope,
            next_cursor="next",
            changes_cursor="changes",
        )
    with pytest.raises(ValidationError):
        CatalogSyncDescriptor(
            collection_id=1,
            archive_root_sha256="c" * 64,
            content_identity="d" * 64,
            revision="0",
        )
    boundary = CatalogSyncDescriptor(
        collection_id=1,
        archive_root_sha256="c" * 64,
        content_identity="d" * 64,
        revision=str(MAX_CATALOG_SYNC_REVISION),
    )
    assert boundary.revision == str(MAX_CATALOG_SYNC_REVISION)
    with pytest.raises(ValidationError):
        CatalogSyncDescriptor(
            collection_id=1,
            archive_root_sha256="c" * 64,
            content_identity="d" * 64,
            revision=str(MAX_CATALOG_SYNC_REVISION + 1),
        )

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from http_api_contracts import BrowseTokenCodec
from riverhog_age import decrypt_age_scrypt
from riverhog_application_access import ApplicationAccess
from riverhog_core.app_permissions import (
    ALL_RESOURCES,
    CATALOG_READ,
    COLLECTION_TAGS_MANAGE,
    ApplicationPrincipal,
    tag_resource,
)
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_events import (
    begin_catalog_event,
    open_catalog_tag_visibility,
    publish_catalog_event,
)
from riverhog_core.catalog_models import (
    CatalogEventRecord,
    CollectionRecord,
    CollectionTagMembershipRecord,
    CollectionTagMutationNodeReferenceRecord,
    CollectionTagMutationRecord,
    CollectionTagNodeGcRecord,
    CollectionTagNodeRecord,
    CollectionTagPublicationFrontierRecord,
    CollectionTagPublicationRecord,
    CollectionTagPublishedNodeRecord,
    CollectionTagRecord,
    CollectionTagRevisionRecord,
    CollectionTagVisibilityRecord,
)
from riverhog_core.ports.archive_store import CollectionTagObjectReceipt
from riverhog_core.runtime_config import DEV_ARCHIVE_PASSPHRASE, RuntimeConfig
from riverhog_core.services.catalog_sync import (
    SqlAlchemyCatalogSyncService,
    _reap_unreferenced_tag_history,
)
from riverhog_core.services.collection_tags import (
    SqlAlchemyCollectionTagService,
    build_collection_tag_set,
)
from riverhog_protocol import (
    COLLECTION_TAG_HEAD_RELATIVE_PATH,
    COLLECTION_TAG_UTF8_BYTES_MAX,
    CatalogSyncDelete,
    CollectionTagHeadDocument,
    CollectionTagSet,
    CollectionTagSetRoot,
    collection_tag_node_path,
    collection_tag_sha256,
)
from riverhog_protocol.errors import NotFound, PreconditionFailed, ServiceUnavailable
from sqlalchemy import exists, select
from time_formats import utc_timestamp_now

from tests.unit.archive_object_fixtures import (
    MemoryArchiveStore,
    archive_store_binding,
    seed_archive_copy,
)
from tests.unit.db_helpers import sqlite_url


def _principal(*tags: str) -> ApplicationPrincipal:
    return ApplicationPrincipal(
        app="tag-editor",
        key_id="tag-editor-key",
        access=frozenset(
            ApplicationAccess(permission, tag_resource(tag))
            for permission in (CATALOG_READ, COLLECTION_TAGS_MANAGE)
            for tag in tags
        ),
    )


def _service(
    path: Path,
    *,
    archive_store: MemoryArchiveStore | None = None,
) -> tuple[SqlAlchemyCollectionTagService, object, MemoryArchiveStore]:
    config, archive = seed_archive_copy(path, {"camera/clip.bin": b"clip"}, store="archive")
    store = archive_store or MemoryArchiveStore()
    factory = make_session_factory(config.database_url)
    with session_scope(factory) as session:
        collection = session.get(CollectionRecord, archive.collection_id)
        assert collection is not None and collection.archive_root_sha256 is not None
        tag_set, _created = build_collection_tag_set(session, ("source:camera",))
        head = CollectionTagHeadDocument.seal(
            archive_root_sha256=collection.archive_root_sha256,
            revision=1,
            root_sha256=tag_set.root.root_sha256,
        )
        collection.tag_root_sha256 = head.root_sha256
        collection.tag_set_identity = head.tag_set_identity
        collection.tag_head_identity = head.head_identity
        revision = session.get(CollectionTagRevisionRecord, (archive.collection_id, 1))
        assert revision is not None
        revision.root_sha256 = head.root_sha256
        revision.tag_set_identity = head.tag_set_identity
        revision.head_identity = head.head_identity
        digest = collection_tag_sha256("source:camera")
        session.add(
            CollectionTagRecord(
                tag_sha256=digest,
                tag="source:camera",
                search_text="source:camera",
                created_at=utc_timestamp_now(),
                updated_at=utc_timestamp_now(),
                collection_count=1,
            )
        )
        session.flush()
        session.add(
            CollectionTagMembershipRecord(
                collection_id=archive.collection_id,
                tag_sha256=digest,
                added_at=utc_timestamp_now(),
            )
        )
        open_catalog_tag_visibility(
            session,
            collection_id=archive.collection_id,
            tag_sha256=digest,
            revision=1,
        )
        publication = session.get(
            CollectionTagPublicationRecord, (archive.collection_id, "archive")
        )
        assert publication is not None
        publication.desired_revision = head.revision
        publication.desired_tag_set_identity = head.tag_set_identity
        publication.desired_head_identity = head.head_identity
        publication.published_revision = head.revision
        publication.published_tag_set_identity = head.tag_set_identity
        publication.published_head_identity = head.head_identity
    receipt = store.publish_collection_tag_head(
        collection_id=archive.collection_id,
        archive_storage_prefix="archives/archive/opaque-docs",
        document=head.to_json_bytes(),
        passphrase_id="riverhog-dev-key-v1",
    )
    with session_scope(factory) as session:
        publication = session.get(
            CollectionTagPublicationRecord, (archive.collection_id, "archive")
        )
        assert publication is not None
        publication.head_object_path = receipt.object_path
        publication.head_provider_revision = receipt.revision
        publication.head_stored_bytes = receipt.stored_bytes
        publication.head_stored_sha256 = receipt.stored_sha256
        publication.published_at = receipt.published_at
    service = SqlAlchemyCollectionTagService(
        config,
        ArchiveStoreRegistry({"archive": archive_store_binding(store)}),
        session_factory=factory,
    )
    return service, factory, store


class _EncryptedMemoryTagNodes:
    def __init__(self, store: MemoryArchiveStore, prefix: str) -> None:
        self.store = store
        self.prefix = prefix

    def get(self, digest: str) -> bytes:
        path = f"{self.prefix}/{collection_tag_node_path(digest)}"
        return decrypt_age_scrypt(self.store.objects[path], DEV_ARCHIVE_PASSPHRASE)

    def put(self, digest: str, encoded: bytes) -> None:
        raise AssertionError((digest, encoded))


def _recover_stored_tags(store: MemoryArchiveStore) -> tuple[CollectionTagHeadDocument, set[str]]:
    prefix = "archives/archive/opaque-docs"
    head = CollectionTagHeadDocument.from_json_bytes(
        decrypt_age_scrypt(
            store.objects[f"{prefix}/{COLLECTION_TAG_HEAD_RELATIVE_PATH}"],
            DEV_ARCHIVE_PASSPHRASE,
        )
    )
    tags = CollectionTagSet(
        _EncryptedMemoryTagNodes(store, prefix),
        CollectionTagSetRoot.seal(head.root_sha256),
    )
    return head, set(tags.iter_tags())


def test_tag_mutation_is_exact_replayable_and_aba_safe(tmp_path: Path) -> None:
    service, factory, _store = _service(tmp_path / "catalog.sqlite3")
    principal = _principal("source:camera", "workflow:archive")
    with session_scope(factory) as session:  # type: ignore[arg-type]
        collection = session.get(CollectionRecord, 1)
        assert collection is not None
        initial_identity = collection.tag_set_identity

    added = service.add(
        1,
        tag="workflow:archive",
        operation_id="add-workflow",
        expected_revision=1,
        expected_tag_set_identity=initial_identity,
        principal=principal,
    )
    assert added["changed"] is True
    assert added["revision"] == 2
    assert (
        service.add(
            1,
            tag="workflow:archive",
            operation_id="add-workflow",
            expected_revision=1,
            expected_tag_set_identity=initial_identity,
            principal=principal,
        )
        == added
    )

    removed = service.remove(
        1,
        tag="workflow:archive",
        operation_id="remove-workflow",
        expected_revision=2,
        expected_tag_set_identity=str(added["tag_set_identity"]),
        principal=principal,
    )
    assert removed["revision"] == 3
    assert removed["tag_set_identity"] == initial_identity
    with pytest.raises(PreconditionFailed):
        service.add(
            1,
            tag="workflow:archive",
            operation_id="stale-after-aba",
            expected_revision=1,
            expected_tag_set_identity=initial_identity,
            principal=principal,
        )

    with session_scope(factory) as session:  # type: ignore[arg-type]
        revisions = list(
            session.scalars(
                select(CollectionTagRevisionRecord.revision).order_by(
                    CollectionTagRevisionRecord.revision
                )
            )
        )
    assert revisions == [1, 2, 3]


def test_tag_addition_cannot_grant_its_own_collection_access(tmp_path: Path) -> None:
    service, factory, _store = _service(tmp_path / "catalog.sqlite3")
    with session_scope(factory) as session:  # type: ignore[arg-type]
        collection = session.get(CollectionRecord, 1)
        assert collection is not None
        identity = collection.tag_set_identity

    with pytest.raises(NotFound):
        service.add(
            1,
            tag="workflow:archive",
            operation_id="self-authorizing-add",
            expected_revision=1,
            expected_tag_set_identity=identity,
            principal=_principal("workflow:archive"),
        )


def test_tag_removal_emits_exact_loss_of_visibility_without_event_tag_snapshots(
    tmp_path: Path,
) -> None:
    path = tmp_path / "catalog.sqlite3"
    tags, factory, _store = _service(path)
    principal = _principal("source:camera")
    catalog = SqlAlchemyCatalogSyncService(
        RuntimeConfig(
            database_url=sqlite_url(path),
            browse_token_signing_key="catalog-tag-visibility-test-key-v1",
        ),
        session_factory=factory,
    )
    with session_scope(factory) as session:  # type: ignore[arg-type]
        collection = session.get(CollectionRecord, 1)
        assert collection is not None
        event = begin_catalog_event(
            session,
            change="created",
            collection_id=1,
            occurred_at=utc_timestamp_now(),
            inventory_identity=collection.inventory_identity,
            before_tag_revision=None,
            after_tag_revision=1,
        )
        publish_catalog_event(session, event=event)
    checkpoint = catalog.checkpoint(principal=principal)
    baseline = catalog.collections(
        cursor=checkpoint.catalog_cursor,
        limit=1,
        principal=principal,
    )
    assert [item.collection_id for item in baseline.collections] == [1]
    assert baseline.changes_cursor is not None

    removed = tags.remove(
        1,
        tag="source:camera",
        operation_id="remove-own-visibility",
        expected_revision=1,
        expected_tag_set_identity=baseline.collections[0].tag_set_identity,
        principal=principal,
    )
    assert removed["revision"] == 2
    catchup = catalog.changes(
        cursor=baseline.changes_cursor,
        limit=1,
        principal=principal,
    )
    assert catchup.changes == [] and catchup.caught_up is True
    changes = catalog.changes(
        cursor=catchup.next_cursor,
        limit=1,
        principal=principal,
    )

    assert changes.changes == [CatalogSyncDelete(collection_id=1, revision="2")]
    with session_scope(factory) as session:  # type: ignore[arg-type]
        intervals = list(session.scalars(select(CollectionTagVisibilityRecord)))
        assert len(intervals) == 1
        assert intervals[0].start_revision == 1
        assert intervals[0].end_revision == 2


def test_tag_exact_revision_membership_and_bounded_pages(tmp_path: Path) -> None:
    service, factory, _store = _service(tmp_path / "catalog.sqlite3")
    principal = _principal("source:camera", "workflow:archive")
    with session_scope(factory) as session:  # type: ignore[arg-type]
        collection = session.get(CollectionRecord, 1)
        assert collection is not None
        initial_identity = collection.tag_set_identity
    added = service.add(
        1,
        tag="workflow:archive",
        operation_id="add-workflow",
        expected_revision=1,
        expected_tag_set_identity=initial_identity,
        principal=principal,
    )

    assert (
        service.contains(
            1,
            tag="workflow:archive",
            revision=2,
            tag_set_identity=str(added["tag_set_identity"]),
            principal=principal,
        )["present"]
        is True
    )
    first = service.list_collection(
        1,
        page_size=1,
        position=None,
        expected_revision=2,
        expected_tag_set_identity=str(added["tag_set_identity"]),
        principal=principal,
    )
    assert len(first["tags"]) == 1
    assert first["_next_position"] is not None
    second = service.list_collection(
        1,
        page_size=1,
        position=first["_next_position"],  # type: ignore[arg-type]
        expected_revision=2,
        expected_tag_set_identity=str(added["tag_set_identity"]),
        principal=principal,
    )
    assert set(first["tags"]) | set(second["tags"]) == {
        "source:camera",
        "workflow:archive",
    }


def test_pending_mutation_nodes_survive_maintenance_and_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "catalog.sqlite3"
    service, factory, store = _service(path)
    principal = _principal("source:camera", "workflow:archive")
    with session_scope(factory) as session:  # type: ignore[arg-type]
        collection = session.get(CollectionRecord, 1)
        assert collection is not None
        initial_identity = collection.tag_set_identity

    entered = threading.Event()
    resume = threading.Event()
    finish = service._finish_mutation

    def paused_finish(collection_id: int, operation_id: str) -> None:
        entered.set()
        assert resume.wait(timeout=10)
        finish(collection_id, operation_id)

    monkeypatch.setattr(service, "_finish_mutation", paused_finish)
    catalog = SqlAlchemyCatalogSyncService(
        RuntimeConfig(database_url=sqlite_url(path)),
        session_factory=factory,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        mutation = executor.submit(
            service.add,
            1,
            tag="workflow:archive",
            operation_id="pause-after-construction",
            expected_revision=1,
            expected_tag_set_identity=initial_identity,
            principal=principal,
        )
        assert entered.wait(timeout=10)
        with session_scope(factory) as session:  # type: ignore[arg-type]
            pending = session.get(
                CollectionTagMutationRecord,
                (1, "pause-after-construction"),
            )
            protected = set(
                session.scalars(
                    select(CollectionTagMutationNodeReferenceRecord.node_digest).where(
                        CollectionTagMutationNodeReferenceRecord.collection_id == 1,
                        CollectionTagMutationNodeReferenceRecord.operation_id
                        == "pause-after-construction",
                    )
                )
            )
            assert pending is not None and pending.state == "pending"
            assert protected

        assert catalog.reap_expired_history(limit=100) == 0
        with session_scope(factory) as session:  # type: ignore[arg-type]
            assert protected <= set(session.scalars(select(CollectionTagNodeRecord.digest)))
            assert protected == set(
                session.scalars(
                    select(CollectionTagMutationNodeReferenceRecord.node_digest).where(
                        CollectionTagMutationNodeReferenceRecord.collection_id == 1,
                        CollectionTagMutationNodeReferenceRecord.operation_id
                        == "pause-after-construction",
                    )
                )
            )
        resume.set()
        result = mutation.result(timeout=10)

    restarted = SqlAlchemyCollectionTagService(
        RuntimeConfig(database_url=sqlite_url(path)),
        ArchiveStoreRegistry({"archive": archive_store_binding(store)}),
        session_factory=make_session_factory(sqlite_url(path)),
    )
    first = restarted.list_collection(
        1,
        page_size=1,
        position=None,
        expected_revision=2,
        expected_tag_set_identity=str(result["tag_set_identity"]),
        principal=principal,
    )
    second = restarted.list_collection(
        1,
        page_size=1,
        position=first["_next_position"],  # type: ignore[arg-type]
        expected_revision=2,
        expected_tag_set_identity=str(result["tag_set_identity"]),
        principal=principal,
    )
    assert set(first["tags"]) | set(second["tags"]) == {
        "source:camera",
        "workflow:archive",
    }
    head, recovered = _recover_stored_tags(store)
    assert head.revision == 2
    assert recovered == {"source:camera", "workflow:archive"}

    catalog.reap_expired_history(limit=100)
    with session_scope(factory) as session:  # type: ignore[arg-type]
        assert not list(session.scalars(select(CollectionTagMutationNodeReferenceRecord)))


def test_pending_mutation_reuses_a_retiring_root_without_a_retention_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "catalog.sqlite3"
    service, factory, store = _service(path)
    principal = _principal("source:camera", "workflow:archive")
    with session_scope(factory) as session:  # type: ignore[arg-type]
        collection = session.get(CollectionRecord, 1)
        assert collection is not None
        initial_identity = collection.tag_set_identity
        initial_root = collection.tag_root_sha256
    added = service.add(
        1,
        tag="workflow:archive",
        operation_id="establish-intermediate-root",
        expected_revision=1,
        expected_tag_set_identity=initial_identity,
        principal=principal,
    )
    with session_scope(factory) as session:  # type: ignore[arg-type]
        retired = session.get(CollectionTagRevisionRecord, (1, 1))
        assert retired is not None and retired.root_sha256 == initial_root
        retired.cleanup_started_at = "2026-01-01T00:00:00.000000Z"

    entered = threading.Event()
    resume = threading.Event()
    finish = service._finish_mutation

    def paused_finish(collection_id: int, operation_id: str) -> None:
        entered.set()
        assert resume.wait(timeout=10)
        finish(collection_id, operation_id)

    monkeypatch.setattr(service, "_finish_mutation", paused_finish)
    with ThreadPoolExecutor(max_workers=1) as executor:
        mutation = executor.submit(
            service.remove,
            1,
            tag="workflow:archive",
            operation_id="reuse-retiring-root",
            expected_revision=2,
            expected_tag_set_identity=str(added["tag_set_identity"]),
            principal=principal,
        )
        assert entered.wait(timeout=10)
        with session_scope(factory) as session:  # type: ignore[arg-type]
            pending = session.get(CollectionTagMutationRecord, (1, "reuse-retiring-root"))
            assert pending is not None and pending.result_root_sha256 == initial_root
            assert session.get(CollectionTagNodeRecord, initial_root) is not None
            assert session.scalar(
                select(
                    exists().where(
                        CollectionTagPublicationFrontierRecord.collection_id == 1,
                        CollectionTagPublicationFrontierRecord.head_identity
                        == pending.result_head_identity,
                        CollectionTagPublicationFrontierRecord.node_digest == initial_root,
                    )
                )
            )

        with session_scope(factory) as session:  # type: ignore[arg-type]
            _reap_unreferenced_tag_history(
                session,
                limit=1_000,
                cleanup_before="2026-02-01T00:00:00.000000Z",
                cleanup_started_at="2026-02-01T00:00:00.000000Z",
            )
        with session_scope(factory) as session:  # type: ignore[arg-type]
            assert session.get(CollectionTagRevisionRecord, (1, 1)) is None
            assert session.get(CollectionTagNodeRecord, initial_root) is not None
        resume.set()
        result = mutation.result(timeout=10)

    assert result["revision"] == 3
    assert result["tag_set_identity"] == initial_identity
    head, recovered = _recover_stored_tags(store)
    assert head.revision == 3
    assert recovered == {"source:camera"}


def test_maximum_length_tag_is_a_nonfinal_browse_page(tmp_path: Path) -> None:
    service, factory, _store = _service(tmp_path / "catalog.sqlite3")
    maximum = "m" * COLLECTION_TAG_UTF8_BYTES_MAX
    maximum_digest = collection_tag_sha256(maximum)
    candidates = (f"short/{index}" for index in range(10_000))
    before = next(tag for tag in candidates if collection_tag_sha256(tag) < maximum_digest)
    after = next(tag for tag in candidates if collection_tag_sha256(tag) > maximum_digest)
    with session_scope(factory) as session:  # type: ignore[arg-type]
        tag_set, _created = build_collection_tag_set(session, (before, maximum, after))
        revision = session.get(CollectionTagRevisionRecord, (1, 1))
        assert revision is not None
        revision.root_sha256 = tag_set.root.root_sha256
        revision.tag_set_identity = tag_set.identity
    principal = ApplicationPrincipal(
        app="catalog-reader",
        key_id="catalog-reader-key",
        access=frozenset({ApplicationAccess(CATALOG_READ, ALL_RESOURCES)}),
    )

    first = service.list_collection(
        1,
        page_size=1,
        position=None,
        expected_revision=1,
        expected_tag_set_identity=tag_set.identity,
        principal=principal,
    )
    second = service.list_collection(
        1,
        page_size=1,
        position=first["_next_position"],  # type: ignore[arg-type]
        expected_revision=1,
        expected_tag_set_identity=tag_set.identity,
        principal=principal,
    )
    third = service.list_collection(
        1,
        page_size=1,
        position=second["_next_position"],  # type: ignore[arg-type]
        expected_revision=1,
        expected_tag_set_identity=tag_set.identity,
        principal=principal,
    )

    assert second["tags"] == [maximum]
    assert second["_next_position"] == (maximum_digest,)
    assert third["tags"] == [after]
    assert third["_next_position"] is None


def test_provider_nodes_for_retained_exact_revisions_remain_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, factory, store = _service(tmp_path / "catalog.sqlite3")
    principal = _principal("source:camera", "workflow:archive")
    with session_scope(factory) as session:  # type: ignore[arg-type]
        collection = session.get(CollectionRecord, 1)
        assert collection is not None
        initial_identity = collection.tag_set_identity
    added = service.add(
        1,
        tag="workflow:archive",
        operation_id="add-workflow",
        expected_revision=1,
        expected_tag_set_identity=initial_identity,
        principal=principal,
    )
    service.remove(
        1,
        tag="workflow:archive",
        operation_id="remove-workflow",
        expected_revision=2,
        expected_tag_set_identity=str(added["tag_set_identity"]),
        principal=principal,
    )
    with session_scope(factory) as session:  # type: ignore[arg-type]
        events = list(session.scalars(select(CatalogEventRecord)))
        assert len(events) == 2
        for event in events:
            event.committed_at = "2026-01-01T00:00:00.000000Z"
    monkeypatch.setattr(
        "riverhog_core.services.catalog_sync.utc_now",
        lambda: datetime(2026, 9, 8, tzinfo=UTC),
    )
    catalog = SqlAlchemyCatalogSyncService(
        RuntimeConfig(
            database_url=sqlite_url(tmp_path / "catalog.sqlite3"),
            catalog_sync_history_retention=timedelta(days=1),
            catalog_sync_bootstrap_lifetime=timedelta(hours=1),
            catalog_sync_cursor_lifetime=timedelta(hours=1),
            browse_token_lifetime=timedelta(hours=1),
        ),
        session_factory=factory,
    )
    assert catalog.reap_expired_history(limit=100) == 2
    monkeypatch.setattr(
        "riverhog_core.services.catalog_sync.utc_now",
        lambda: datetime(2026, 9, 8, 1, 0, 1, tzinfo=UTC),
    )
    for _ in range(256):
        catalog.reap_expired_history(limit=100)
        with session_scope(factory) as session:  # type: ignore[arg-type]
            if list(session.scalars(select(CollectionTagRevisionRecord.revision))) == [3]:
                break
    else:  # pragma: no cover - fixed cleanup state is much smaller
        raise AssertionError("retired exact tag authorities did not converge")
    for _ in range(256):
        if service.process_due(limit=1) == 0:
            break
    else:  # pragma: no cover - fixed-depth tag tree is much smaller
        raise AssertionError("tag-node garbage collection did not settle")

    with session_scope(factory) as session:  # type: ignore[arg-type]
        publication = session.get(CollectionTagPublicationRecord, (1, "archive"))
        assert publication is not None and publication.published_head_identity is not None
        current = set(
            session.scalars(
                select(CollectionTagPublicationFrontierRecord.node_digest).where(
                    CollectionTagPublicationFrontierRecord.collection_id == 1,
                    CollectionTagPublicationFrontierRecord.store == "archive",
                    CollectionTagPublicationFrontierRecord.head_identity
                    == publication.published_head_identity,
                )
            )
        )
        published = set(
            session.scalars(
                select(CollectionTagPublishedNodeRecord.node_digest).where(
                    CollectionTagPublishedNodeRecord.collection_id == 1,
                    CollectionTagPublishedNodeRecord.store == "archive",
                )
            )
        )
    assert current == published
    assert {path for path in store.objects if "/tags/nodes/" in path} == {
        f"archives/archive/opaque-docs/{collection_tag_node_path(digest)}" for digest in published
    }
    recovered_head, recovered_tags = _recover_stored_tags(store)
    assert recovered_head.revision == 3
    assert recovered_tags == {"source:camera"}


class _DelayedTagHeadStore(MemoryArchiveStore):
    def __init__(self) -> None:
        super().__init__()
        self.delay_next_head = False
        self.started = threading.Event()
        self.resume = threading.Event()

    def publish_collection_tag_head(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
        document: bytes,
        passphrase_id: str,
        expected_current_stored_sha256: str | None = None,
    ) -> CollectionTagObjectReceipt:
        if self.delay_next_head:
            self.delay_next_head = False
            self.started.set()
            assert self.resume.wait(timeout=10)
        return super().publish_collection_tag_head(
            collection_id=collection_id,
            archive_storage_prefix=archive_storage_prefix,
            document=document,
            passphrase_id=passphrase_id,
            expected_current_stored_sha256=expected_current_stored_sha256,
        )


class _CommittedTagHeadWithoutResponseStore(MemoryArchiveStore):
    def __init__(self) -> None:
        super().__init__()
        self.lose_next_head_response = False

    def publish_collection_tag_head(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
        document: bytes,
        passphrase_id: str,
        expected_current_stored_sha256: str | None = None,
    ) -> CollectionTagObjectReceipt:
        receipt = super().publish_collection_tag_head(
            collection_id=collection_id,
            archive_storage_prefix=archive_storage_prefix,
            document=document,
            passphrase_id=passphrase_id,
            expected_current_stored_sha256=expected_current_stored_sha256,
        )
        if self.lose_next_head_response:
            self.lose_next_head_response = False
            raise OSError("simulated lost response after durable head replacement")
        return receipt


def test_committed_tag_head_reconciles_after_its_response_is_lost(tmp_path: Path) -> None:
    store = _CommittedTagHeadWithoutResponseStore()
    service, factory, _store = _service(tmp_path / "catalog.sqlite3", archive_store=store)
    principal = _principal("source:camera", "workflow:archive")
    with session_scope(factory) as session:  # type: ignore[arg-type]
        collection = session.get(CollectionRecord, 1)
        assert collection is not None
        initial_identity = collection.tag_set_identity
    store.lose_next_head_response = True

    with pytest.raises(ServiceUnavailable):
        service.add(
            1,
            tag="workflow:archive",
            operation_id="lost-head-response",
            expected_revision=1,
            expected_tag_set_identity=initial_identity,
            principal=principal,
        )

    result = service.add(
        1,
        tag="workflow:archive",
        operation_id="lost-head-response",
        expected_revision=1,
        expected_tag_set_identity=initial_identity,
        principal=principal,
    )
    assert result["revision"] == 2
    head, tags = _recover_stored_tags(store)
    assert head.revision == 2
    assert tags == {"source:camera", "workflow:archive"}


def test_delayed_old_head_writer_cannot_overwrite_newer_acknowledged_authority(
    tmp_path: Path,
) -> None:
    store = _DelayedTagHeadStore()
    service, factory, _store = _service(tmp_path / "catalog.sqlite3", archive_store=store)
    principal = _principal("source:camera", "workflow:archive")
    with session_scope(factory) as session:  # type: ignore[arg-type]
        collection = session.get(CollectionRecord, 1)
        assert collection is not None
        initial_identity = collection.tag_set_identity
    store.delay_next_head = True

    with ThreadPoolExecutor(max_workers=1) as executor:
        delayed = executor.submit(
            service.add,
            1,
            tag="workflow:archive",
            operation_id="delayed-add",
            expected_revision=1,
            expected_tag_set_identity=initial_identity,
            principal=principal,
        )
        assert store.started.wait(timeout=10)
        restarted = SqlAlchemyCollectionTagService(
            RuntimeConfig(database_url=sqlite_url(tmp_path / "catalog.sqlite3")),
            ArchiveStoreRegistry({"archive": archive_store_binding(store)}),
            session_factory=factory,
        )
        assert restarted.requeue_interrupted_for_startup(limit=1) == 1
        assert restarted.process_due(limit=1) == 1
        with session_scope(factory) as session:  # type: ignore[arg-type]
            added = session.get(CollectionTagMutationRecord, (1, "delayed-add"))
            assert added is not None and added.state == "succeeded"
            assert added.result_revision == 2
            added_identity = added.result_tag_set_identity
        removed = restarted.remove(
            1,
            tag="workflow:archive",
            operation_id="newer-remove",
            expected_revision=2,
            expected_tag_set_identity=added_identity,
            principal=principal,
        )
        assert removed["revision"] == 3
        store.resume.set()
        delayed.result(timeout=10)

    head, tags = _recover_stored_tags(store)
    assert head.revision == 3
    assert tags == {"source:camera"}


class _DelayedTagDeleteStore(MemoryArchiveStore):
    def __init__(self) -> None:
        super().__init__()
        self.delay_next_delete = False
        self.started = threading.Event()
        self.resume = threading.Event()

    def delete_collection_tag_node(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
        digest: str,
        expected_current_stored_sha256: str,
        provider_revision: str | None,
    ) -> None:
        if self.delay_next_delete:
            self.delay_next_delete = False
            self.started.set()
            assert self.resume.wait(timeout=10)
        super().delete_collection_tag_node(
            collection_id=collection_id,
            archive_storage_prefix=archive_storage_prefix,
            digest=digest,
            expected_current_stored_sha256=expected_current_stored_sha256,
            provider_revision=provider_revision,
        )


def test_delayed_gc_cannot_delete_a_node_republished_by_a_newer_authority(
    tmp_path: Path,
) -> None:
    path = tmp_path / "catalog.sqlite3"
    store = _DelayedTagDeleteStore()
    service, factory, _store = _service(path, archive_store=store)
    principal = _principal("source:camera", "workflow:archive")
    with session_scope(factory) as session:  # type: ignore[arg-type]
        collection = session.get(CollectionRecord, 1)
        assert collection is not None
        initial_identity = collection.tag_set_identity
    added = service.add(
        1,
        tag="workflow:archive",
        operation_id="add-before-gc",
        expected_revision=1,
        expected_tag_set_identity=initial_identity,
        principal=principal,
    )
    service.remove(
        1,
        tag="workflow:archive",
        operation_id="remove-before-gc",
        expected_revision=2,
        expected_tag_set_identity=str(added["tag_set_identity"]),
        principal=principal,
    )
    for _ in range(256):
        if service.process_due(limit=1) == 0:
            break
    with session_scope(factory) as session:  # type: ignore[arg-type]
        publication = session.get(CollectionTagPublicationRecord, (1, "archive"))
        assert publication is not None and publication.published_head_identity is not None
        session.query(CollectionTagPublicationFrontierRecord).filter(
            CollectionTagPublicationFrontierRecord.collection_id == 1,
            CollectionTagPublicationFrontierRecord.store == "archive",
            CollectionTagPublicationFrontierRecord.head_identity
            != publication.published_head_identity,
        ).delete(synchronize_session=False)

    store.delay_next_delete = True
    with ThreadPoolExecutor(max_workers=1) as executor:
        delayed = executor.submit(service.process_due, limit=1)
        assert store.started.wait(timeout=10)
        restarted = SqlAlchemyCollectionTagService(
            RuntimeConfig(database_url=sqlite_url(path)),
            ArchiveStoreRegistry({"archive": archive_store_binding(store)}),
            session_factory=factory,
        )
        assert restarted.requeue_interrupted_for_startup(limit=1) == 1
        assert restarted.process_due(limit=1) == 1
        readded = restarted.add(
            1,
            tag="workflow:archive",
            operation_id="readd-after-gc",
            expected_revision=3,
            expected_tag_set_identity=initial_identity,
            principal=principal,
        )
        assert readded["revision"] == 4
        store.resume.set()
        assert delayed.result(timeout=10) == 1

    head, tags = _recover_stored_tags(store)
    assert head.revision == 4
    assert tags == {"source:camera", "workflow:archive"}


class _AmbiguousTagDeleteStore(MemoryArchiveStore):
    fail_delete_once = True

    def delete_collection_tag_node(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
        digest: str,
        expected_current_stored_sha256: str,
        provider_revision: str | None,
    ) -> None:
        super().delete_collection_tag_node(
            collection_id=collection_id,
            archive_storage_prefix=archive_storage_prefix,
            digest=digest,
            expected_current_stored_sha256=expected_current_stored_sha256,
            provider_revision=provider_revision,
        )
        if self.fail_delete_once:
            self.fail_delete_once = False
            raise RuntimeError("ambiguous provider response")


def test_tag_node_gc_resumes_idempotently_after_an_ambiguous_delete(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    store = _AmbiguousTagDeleteStore()
    service, factory, _store = _service(path, archive_store=store)
    principal = _principal("source:camera", "workflow:archive")
    with session_scope(factory) as session:  # type: ignore[arg-type]
        collection = session.get(CollectionRecord, 1)
        assert collection is not None
        initial_identity = collection.tag_set_identity
    added = service.add(
        1,
        tag="workflow:archive",
        operation_id="add-workflow",
        expected_revision=1,
        expected_tag_set_identity=initial_identity,
        principal=principal,
    )
    service.remove(
        1,
        tag="workflow:archive",
        operation_id="remove-workflow",
        expected_revision=2,
        expected_tag_set_identity=str(added["tag_set_identity"]),
        principal=principal,
    )
    for _ in range(256):
        if service.process_due(limit=1) == 0:
            break
    with session_scope(factory) as session:  # type: ignore[arg-type]
        publication = session.get(CollectionTagPublicationRecord, (1, "archive"))
        assert publication is not None and publication.published_head_identity is not None
        session.query(CollectionTagPublicationFrontierRecord).filter(
            CollectionTagPublicationFrontierRecord.collection_id == 1,
            CollectionTagPublicationFrontierRecord.store == "archive",
            CollectionTagPublicationFrontierRecord.head_identity
            != publication.published_head_identity,
        ).delete(synchronize_session=False)
    assert service.process_due(limit=1) == 1
    with session_scope(factory) as session:  # type: ignore[arg-type]
        gc = session.scalar(select(CollectionTagNodeGcRecord))
        assert gc is not None and gc.state == "retry_wait"
        gc.next_attempt_at = utc_timestamp_now()
    restarted = SqlAlchemyCollectionTagService(
        RuntimeConfig(database_url=sqlite_url(path)),
        ArchiveStoreRegistry({"archive": archive_store_binding(store)}),
        session_factory=factory,
    )
    for _ in range(256):
        if restarted.process_due(limit=1) == 0:
            break
    with session_scope(factory) as session:  # type: ignore[arg-type]
        assert session.scalar(select(CollectionTagNodeGcRecord)) is None


def test_tag_history_cleanup_bounds_all_subordinate_rows_and_restarts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "catalog.sqlite3"
    service, factory, _store = _service(path)
    principal = _principal("source:camera", "workflow:archive")
    with session_scope(factory) as session:  # type: ignore[arg-type]
        collection = session.get(CollectionRecord, 1)
        assert collection is not None
        initial_identity = collection.tag_set_identity
    service.add(
        1,
        tag="workflow:archive",
        operation_id="make-initial-revision-retired",
        expected_revision=1,
        expected_tag_set_identity=initial_identity,
        principal=principal,
    )

    frontier_rows = 4_096
    terminal_mutations = 257
    with session_scope(factory) as session:  # type: ignore[arg-type]
        retired = session.get(CollectionTagRevisionRecord, (1, 1))
        assert retired is not None
        retired_head_identity = retired.head_identity
        retired.cleanup_started_at = utc_timestamp_now()
        session.query(CollectionTagMutationNodeReferenceRecord).delete(synchronize_session=False)
        session.add_all(
            CollectionTagPublicationFrontierRecord(
                collection_id=1,
                store="archive",
                head_identity=retired.head_identity,
                node_digest=f"{index + 1:064x}",
                expanded=True,
                published=True,
            )
            for index in range(frontier_rows)
        )
        tag_digest = collection_tag_sha256("source:camera")
        now = utc_timestamp_now()
        session.add_all(
            CollectionTagMutationRecord(
                collection_id=1,
                operation_id=f"retired-noop-{index:04d}",
                action="add",
                tag="source:camera",
                tag_sha256=tag_digest,
                expected_revision=1,
                expected_tag_set_identity=retired.tag_set_identity,
                result_revision=1,
                result_root_sha256=retired.root_sha256,
                result_tag_set_identity=retired.tag_set_identity,
                result_head_identity=retired.head_identity,
                changed=False,
                state="succeeded",
                initiated_by_app="fixture",
                initiated_by_key_id="fixture-key",
                created_at=now,
                updated_at=now,
                failure=None,
            )
            for index in range(terminal_mutations)
        )

    steps = 0
    work_limit = 1
    while True:
        with session_scope(factory) as session:  # type: ignore[arg-type]
            if session.get(CollectionTagRevisionRecord, (1, 1)) is None:
                break
            metrics = _reap_unreferenced_tag_history(
                session,
                limit=work_limit,
                cleanup_before="9999-12-31T23:59:59.999999Z",
                cleanup_started_at="9999-12-31T23:59:59.999999Z",
            )
            assert 1 <= metrics.selected_rows <= work_limit
            assert metrics.locked_rows == metrics.selected_rows
            assert metrics.changed_rows == metrics.selected_rows
            assert metrics.deleted_rows == metrics.changed_rows
        steps += metrics.changed_rows
        work_limit = 97
    assert steps == frontier_rows + terminal_mutations + 1
    with session_scope(factory) as session:  # type: ignore[arg-type]
        assert not list(
            session.scalars(
                select(CollectionTagPublicationFrontierRecord).where(
                    CollectionTagPublicationFrontierRecord.head_identity == retired_head_identity
                )
            )
        )
        assert not list(
            session.scalars(
                select(CollectionTagMutationRecord).where(
                    CollectionTagMutationRecord.result_revision == 1
                )
            )
        )


def test_exact_tag_revisions_expire_with_the_catalog_history_that_names_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "catalog.sqlite3"
    service, factory, _store = _service(path)
    principal = _principal("source:camera", "workflow:archive")
    with session_scope(factory) as session:  # type: ignore[arg-type]
        collection = session.get(CollectionRecord, 1)
        assert collection is not None
        initial_identity = collection.tag_set_identity
    added = service.add(
        1,
        tag="workflow:archive",
        operation_id="add-workflow",
        expected_revision=1,
        expected_tag_set_identity=initial_identity,
        principal=principal,
    )
    service.remove(
        1,
        tag="workflow:archive",
        operation_id="remove-workflow",
        expected_revision=2,
        expected_tag_set_identity=str(added["tag_set_identity"]),
        principal=principal,
    )
    assert (
        service.contains(
            1,
            tag="workflow:archive",
            revision=2,
            tag_set_identity=str(added["tag_set_identity"]),
            principal=principal,
        )["present"]
        is True
    )
    with session_scope(factory) as session:  # type: ignore[arg-type]
        events = list(
            session.scalars(select(CatalogEventRecord).order_by(CatalogEventRecord.revision))
        )
        assert len(events) == 2
        events[0].committed_at = "2026-01-01T00:00:00.000000Z"
        events[1].committed_at = "2026-09-07T00:00:00.000000Z"
    monkeypatch.setattr(
        "riverhog_core.services.catalog_sync.utc_now",
        lambda: datetime(2026, 9, 8, tzinfo=UTC),
    )
    catalog = SqlAlchemyCatalogSyncService(
        RuntimeConfig(
            database_url=sqlite_url(path),
            catalog_sync_history_retention=timedelta(days=1),
            catalog_sync_bootstrap_lifetime=timedelta(hours=1),
            catalog_sync_cursor_lifetime=timedelta(hours=1),
            browse_token_lifetime=timedelta(hours=1),
        ),
        session_factory=factory,
    )
    assert catalog.reap_expired_history(limit=1) == 1
    for _ in range(32):
        assert catalog.reap_expired_history(limit=1) == 0
    restarted = SqlAlchemyCollectionTagService(
        RuntimeConfig(database_url=sqlite_url(path)),
        ArchiveStoreRegistry({"archive": archive_store_binding(_store)}),
        session_factory=make_session_factory(sqlite_url(path)),
    )
    first = restarted.list_collection(
        1,
        page_size=1,
        position=None,
        expected_revision=2,
        expected_tag_set_identity=str(added["tag_set_identity"]),
        principal=principal,
    )
    browse_now = [datetime(2026, 9, 8, tzinfo=UTC).timestamp()]
    browse_tokens = BrowseTokenCodec(
        b"exact-tag-revision-lifetime-test-key",
        lifetime_seconds=60 * 60,
        clock=lambda: browse_now[0],
    )
    first_token = browse_tokens.issue(
        operation="list_collection_tags",
        principal="reader",
        selectors={"collection_id": 1, "revision": 2},
        position=first["_next_position"],  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        "riverhog_core.services.catalog_sync.utc_now",
        lambda: datetime(2026, 9, 8, 0, 0, 1, tzinfo=UTC),
    )
    assert catalog.reap_expired_history(limit=1) == 1
    assert catalog.reap_expired_history(limit=1) == 0
    with pytest.raises(PreconditionFailed):
        restarted.list_collection(
            1,
            page_size=1,
            position=None,
            expected_revision=2,
            expected_tag_set_identity=str(added["tag_set_identity"]),
            principal=principal,
        )
    monkeypatch.setattr(
        "riverhog_core.services.collection_tags.utc_timestamp_now",
        lambda: "2026-09-08T00:59:59.000000Z",
    )
    browse_now[0] += (60 * 60) - 1
    continuation = browse_tokens.verify(
        first_token,
        operation="list_collection_tags",
        principal="reader",
        selectors={"collection_id": 1, "revision": 2},
    )
    second = restarted.list_collection(
        1,
        page_size=1,
        position=continuation,
        expected_revision=2,
        expected_tag_set_identity=str(added["tag_set_identity"]),
        principal=principal,
    )
    assert set(first["tags"]) | set(second["tags"]) == {
        "source:camera",
        "workflow:archive",
    }

    monkeypatch.setattr(
        "riverhog_core.services.catalog_sync.utc_now",
        lambda: datetime(2026, 9, 8, 1, 0, 2, tzinfo=UTC),
    )
    catalog.reap_expired_history(limit=100)
    with session_scope(factory) as session:  # type: ignore[arg-type]
        assert session.get(CollectionTagRevisionRecord, (1, 2)) is not None

    monkeypatch.setattr(
        "riverhog_core.services.catalog_sync.utc_now",
        lambda: datetime(2026, 9, 8, 2, 0, tzinfo=UTC),
    )
    for _ in range(256):
        catalog.reap_expired_history(limit=1)
        with session_scope(factory) as session:  # type: ignore[arg-type]
            if session.get(CollectionTagRevisionRecord, (1, 2)) is None:
                break
    else:  # pragma: no cover - fixed cleanup state is much smaller
        raise AssertionError("retired exact tag authority did not converge")
    with pytest.raises(PreconditionFailed):
        restarted.contains(
            1,
            tag="workflow:archive",
            revision=2,
            tag_set_identity=str(added["tag_set_identity"]),
            principal=principal,
        )
    with session_scope(factory) as session:  # type: ignore[arg-type]
        assert list(session.scalars(select(CollectionTagRevisionRecord.revision))) == [3]

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from riverhog_application_access import ApplicationAccess
from riverhog_core.app_permissions import (
    CATALOG_READ,
    COLLECTION_TAGS_MANAGE,
    ApplicationPrincipal,
    tag_resource,
)
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CatalogEventRecord,
    CollectionRecord,
    CollectionTagMembershipRecord,
    CollectionTagNodeGcRecord,
    CollectionTagPublicationFrontierRecord,
    CollectionTagPublicationRecord,
    CollectionTagPublishedNodeRecord,
    CollectionTagRecord,
    CollectionTagRevisionRecord,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.catalog_sync import SqlAlchemyCatalogSyncService
from riverhog_core.services.collection_tags import (
    SqlAlchemyCollectionTagService,
    build_collection_tag_set,
)
from riverhog_protocol import (
    CollectionTagHeadDocument,
    collection_tag_node_path,
    collection_tag_sha256,
)
from riverhog_protocol.errors import NotFound, PreconditionFailed
from sqlalchemy import select
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
    store = archive_store or MemoryArchiveStore()
    service = SqlAlchemyCollectionTagService(
        config,
        ArchiveStoreRegistry({"archive": archive_store_binding(store)}),
        session_factory=factory,
    )
    return service, factory, store


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


def test_obsolete_provider_nodes_are_reclaimed_without_touching_the_current_tree(
    tmp_path: Path,
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
    assert published == current
    assert {path for path in store.objects if "/tags/nodes/" in path} == {
        f"archives/archive/opaque-docs/{collection_tag_node_path(digest)}" for digest in current
    }


class _AmbiguousTagDeleteStore(MemoryArchiveStore):
    fail_delete_once = True

    def delete_collection_tag_node(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
        digest: str,
    ) -> None:
        super().delete_collection_tag_node(
            collection_id=collection_id,
            archive_storage_prefix=archive_storage_prefix,
            digest=digest,
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
        events[1].committed_at = "2026-09-08T00:00:00.000000Z"
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
        ),
        session_factory=factory,
    )
    assert catalog.reap_expired_history(limit=1) == 1
    assert catalog.reap_expired_history(limit=1) == 0
    with pytest.raises(PreconditionFailed):
        service.contains(
            1,
            tag="workflow:archive",
            revision=2,
            tag_set_identity=str(added["tag_set_identity"]),
            principal=principal,
        )
    with session_scope(factory) as session:  # type: ignore[arg-type]
        assert list(session.scalars(select(CollectionTagRevisionRecord.revision))) == [3]

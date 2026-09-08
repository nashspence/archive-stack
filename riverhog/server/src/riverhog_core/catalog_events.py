from __future__ import annotations

from collections.abc import Iterable
from typing import Any, cast

from riverhog_protocol import MAX_CATALOG_SYNC_REVISION
from sqlalchemy import case, exists, false, literal, or_, select, true
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement
from time_formats import utc_timestamp_now

from riverhog_core.app_permissions import ALL_RESOURCES, ApplicationPrincipal
from riverhog_core.catalog_models import (
    CatalogEventRecord,
    CatalogSyncStateRecord,
    CollectionRecord,
    CollectionTagVisibilityRecord,
)
from riverhog_core.collection_access import collection_ids, permission_resources, tag_hashes


def record_catalog_event(
    session: Session,
    *,
    change: str,
    collection_id: int,
    occurred_at: str,
    inventory_identity: str,
    before_tags: Iterable[str],
    after_tags: Iterable[str],
) -> CatalogEventRecord:
    collection = _catalog_collection(session, collection_id)
    if tuple(before_tags):
        raise ValueError("fixture-created catalog events have no before visibility")
    event = begin_catalog_event(
        session,
        change=change,
        collection_id=collection_id,
        occurred_at=occurred_at,
        inventory_identity=inventory_identity,
        before_tag_revision=None,
        after_tag_revision=collection.tag_revision,
    )
    for tag_sha256 in sorted(set(after_tags)):
        open_catalog_tag_visibility(
            session,
            collection_id=collection_id,
            tag_sha256=tag_sha256,
            revision=collection.tag_revision,
        )
    publish_catalog_event(session, event=event)
    return event


def begin_catalog_event(
    session: Session,
    *,
    change: str,
    collection_id: int,
    occurred_at: str,
    inventory_identity: str,
    before_tag_revision: int | None,
    after_tag_revision: int | None,
) -> CatalogEventRecord:
    """Create an event bound to relational tag-visibility revisions."""

    collection = _catalog_collection(session, collection_id)
    event = CatalogEventRecord(
        change=change,
        collection_id=collection_id,
        occurred_at=occurred_at,
        inventory_identity=inventory_identity,
        archive_root_sha256=collection.archive_root_sha256,
        content_identity=collection.content_identity,
        description=collection.description,
        description_revision=collection.description_revision,
        description_identity=collection.description_identity,
        tag_revision=collection.tag_revision,
        tag_set_identity=collection.tag_set_identity,
        before_tag_revision=before_tag_revision,
        after_tag_revision=after_tag_revision,
        published=False,
    )
    session.add(event)
    session.flush()
    return event


def publish_catalog_event(
    session: Session,
    *,
    event: CatalogEventRecord,
) -> CatalogEventRecord:
    """Publish one catalog mutation behind a transactionally serialized watermark."""

    if event.published or event.revision is not None:
        raise RuntimeError("catalog event is already published")
    state = session.scalar(
        select(CatalogSyncStateRecord)
        .where(CatalogSyncStateRecord.singleton == 1)
        .with_for_update()
    )
    if state is None:
        raise RuntimeError("catalog synchronization state is unavailable")
    revision = state.committed_revision + 1
    if revision > MAX_CATALOG_SYNC_REVISION:
        raise RuntimeError("catalog synchronization revision domain is exhausted")
    state.committed_revision = revision
    event.revision = revision
    event.committed_at = utc_timestamp_now()
    event.published = True
    collection = session.get(CollectionRecord, event.collection_id)
    if collection is not None:
        collection.catalog_revision = revision
    session.flush()
    return event


def _catalog_collection(session: Session, collection_id: int) -> CollectionRecord:
    collection = session.get(CollectionRecord, collection_id)
    if collection is None or collection.archive_root_sha256 is None:
        raise RuntimeError("catalog collection synchronization identity is unavailable")
    return collection


def open_catalog_tag_visibility(
    session: Session,
    *,
    collection_id: int,
    tag_sha256: str,
    revision: int,
) -> None:
    """Open one membership interval at an exact collection tag revision."""

    active = session.scalar(
        select(CollectionTagVisibilityRecord).where(
            CollectionTagVisibilityRecord.collection_id == collection_id,
            CollectionTagVisibilityRecord.tag_sha256 == tag_sha256,
            CollectionTagVisibilityRecord.end_revision.is_(None),
        )
    )
    if active is not None:
        raise RuntimeError("collection tag visibility interval is already open")
    session.add(
        CollectionTagVisibilityRecord(
            collection_id=collection_id,
            tag_sha256=tag_sha256,
            start_revision=revision,
            end_revision=None,
        )
    )


def close_catalog_tag_visibility(
    session: Session,
    *,
    collection_id: int,
    tag_sha256: str,
    revision: int,
) -> None:
    """Close one active membership interval at an exact collection tag revision."""

    active = session.scalar(
        select(CollectionTagVisibilityRecord)
        .where(
            CollectionTagVisibilityRecord.collection_id == collection_id,
            CollectionTagVisibilityRecord.tag_sha256 == tag_sha256,
            CollectionTagVisibilityRecord.end_revision.is_(None),
        )
        .with_for_update()
    )
    if active is None or revision <= active.start_revision:
        raise RuntimeError("active collection tag visibility interval is unavailable")
    active.end_revision = revision


def catalog_event_projection(
    principal: ApplicationPrincipal | None,
    permission: str,
) -> tuple[ColumnElement[bool], ColumnElement[str]]:
    native_change = cast(ColumnElement[str], CatalogEventRecord.change)
    if principal is None:
        return true(), native_change
    resources = permission_resources(principal, permission)
    if ALL_RESOURCES in resources:
        return true(), native_change

    allowed_collections = collection_ids(resources)
    allowed_tags = tag_hashes(resources)
    exact_match = (
        CatalogEventRecord.collection_id.in_(allowed_collections)
        if allowed_collections
        else false()
    )
    after_match = _tag_revision_match(CatalogEventRecord.after_tag_revision, allowed_tags)
    before_match = _tag_revision_match(CatalogEventRecord.before_tag_revision, allowed_tags)
    remains_visible = or_(exact_match, after_match)
    return (
        or_(remains_visible, before_match),
        case(
            (remains_visible, native_change),
            (before_match, literal("deleted")),
            else_=native_change,
        ),
    )


def _tag_revision_match(revision: Any, allowed_tags: set[str]) -> ColumnElement[bool]:
    if not allowed_tags:
        return false()
    return exists(
        select(1).where(
            CollectionTagVisibilityRecord.collection_id == CatalogEventRecord.collection_id,
            CollectionTagVisibilityRecord.tag_sha256.in_(allowed_tags),
            CollectionTagVisibilityRecord.start_revision <= revision,
            or_(
                CollectionTagVisibilityRecord.end_revision.is_(None),
                CollectionTagVisibilityRecord.end_revision > revision,
            ),
        )
    )


__all__ = [
    "begin_catalog_event",
    "catalog_event_projection",
    "close_catalog_tag_visibility",
    "open_catalog_tag_visibility",
    "publish_catalog_event",
    "record_catalog_event",
]

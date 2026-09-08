from __future__ import annotations

from collections.abc import Iterable
from typing import cast

from riverhog_protocol import MAX_CATALOG_SYNC_REVISION
from sqlalchemy import case, exists, false, insert, literal, or_, select, true
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement
from time_formats import utc_timestamp_now

from riverhog_core.app_permissions import ALL_RESOURCES, ApplicationPrincipal
from riverhog_core.catalog_models import (
    CatalogEventRecord,
    CatalogEventTagRecord,
    CatalogSyncStateRecord,
    CollectionRecord,
    CollectionTagMembershipRecord,
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
        published=False,
    )
    session.add(event)
    session.flush()
    for phase, tags in (("before", before_tags), ("after", after_tags)):
        for tag_sha256 in sorted(set(tags)):
            session.add(
                CatalogEventTagRecord(
                    sequence=event.sequence,
                    phase=phase,
                    tag_sha256=tag_sha256,
                )
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
) -> CatalogEventRecord:
    """Create an event whose tag-visibility snapshots will be populated relationally."""

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


def snapshot_catalog_event_collection_tags(
    session: Session,
    *,
    event: CatalogEventRecord,
    phase: str,
    collection_id: int,
) -> None:
    """Copy exact tag visibility without materializing tag cardinality."""

    if phase not in {"before", "after"}:
        raise ValueError("catalog event tag phase is invalid")
    session.execute(
        insert(CatalogEventTagRecord).from_select(
            ("sequence", "phase", "tag_sha256"),
            select(
                literal(event.sequence),
                literal(phase),
                CollectionTagMembershipRecord.tag_sha256,
            ).where(
                CollectionTagMembershipRecord.collection_id == collection_id,
            ),
        )
    )


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
    after_match = _tag_snapshot_match("after", allowed_tags)
    before_match = _tag_snapshot_match("before", allowed_tags)
    remains_visible = or_(exact_match, after_match)
    return (
        or_(remains_visible, before_match),
        case(
            (remains_visible, native_change),
            (before_match, literal("deleted")),
            else_=native_change,
        ),
    )


def _tag_snapshot_match(phase: str, allowed_tags: set[str]) -> ColumnElement[bool]:
    if not allowed_tags:
        return false()
    return exists(
        select(1).where(
            CatalogEventTagRecord.sequence == CatalogEventRecord.sequence,
            CatalogEventTagRecord.phase == phase,
            CatalogEventTagRecord.tag_sha256.in_(allowed_tags),
        )
    )


__all__ = [
    "begin_catalog_event",
    "catalog_event_projection",
    "publish_catalog_event",
    "record_catalog_event",
    "snapshot_catalog_event_collection_tags",
]

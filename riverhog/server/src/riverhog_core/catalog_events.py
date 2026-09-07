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
    CatalogEventAccessGroupRecord,
    CatalogEventRecord,
    CatalogSyncStateRecord,
    CollectionAccessGroupMembershipRecord,
    CollectionAccessGroupRecord,
    CollectionRecord,
)
from riverhog_core.collection_access import collection_ids, group_ids, permission_resources


def record_catalog_event(
    session: Session,
    *,
    change: str,
    collection_id: int,
    occurred_at: str,
    inventory_identity: str,
    before_groups: Iterable[str],
    after_groups: Iterable[str],
) -> CatalogEventRecord:
    collection = _catalog_collection(session, collection_id)
    event = CatalogEventRecord(
        change=change,
        collection_id=collection_id,
        occurred_at=occurred_at,
        inventory_identity=inventory_identity,
        archive_root_sha256=collection.archive_root_sha256,
        content_identity=collection.content_identity,
        published=False,
    )
    session.add(event)
    session.flush()
    for phase, groups in (("before", before_groups), ("after", after_groups)):
        for group_id in sorted(set(groups)):
            session.add(
                CatalogEventAccessGroupRecord(
                    sequence=event.sequence,
                    phase=phase,
                    group_id=group_id,
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
    """Create an event whose access-group snapshots will be populated relationally."""

    collection = _catalog_collection(session, collection_id)
    event = CatalogEventRecord(
        change=change,
        collection_id=collection_id,
        occurred_at=occurred_at,
        inventory_identity=inventory_identity,
        archive_root_sha256=collection.archive_root_sha256,
        content_identity=collection.content_identity,
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


def snapshot_catalog_event_collection_access_groups(
    session: Session,
    *,
    event: CatalogEventRecord,
    phase: str,
    collection_id: int,
) -> None:
    """Copy active group visibility without materializing group cardinality."""

    if phase not in {"before", "after"}:
        raise ValueError("catalog event access-group phase is invalid")
    session.execute(
        insert(CatalogEventAccessGroupRecord).from_select(
            ("sequence", "phase", "group_id"),
            select(
                literal(event.sequence),
                literal(phase),
                CollectionAccessGroupMembershipRecord.group_id,
            )
            .join(
                CollectionAccessGroupRecord,
                CollectionAccessGroupRecord.id == CollectionAccessGroupMembershipRecord.group_id,
            )
            .where(
                CollectionAccessGroupMembershipRecord.collection_id == collection_id,
                CollectionAccessGroupRecord.status == "active",
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
    allowed_groups = group_ids(resources)
    exact_match = (
        CatalogEventRecord.collection_id.in_(allowed_collections)
        if allowed_collections
        else false()
    )
    after_match = _group_snapshot_match("after", allowed_groups)
    before_match = _group_snapshot_match("before", allowed_groups)
    remains_visible = or_(exact_match, after_match)
    return (
        or_(remains_visible, before_match),
        case(
            (remains_visible, native_change),
            (before_match, literal("deleted")),
            else_=native_change,
        ),
    )


def _group_snapshot_match(phase: str, allowed_groups: set[str]) -> ColumnElement[bool]:
    if not allowed_groups:
        return false()
    return exists(
        select(1).where(
            CatalogEventAccessGroupRecord.sequence == CatalogEventRecord.sequence,
            CatalogEventAccessGroupRecord.phase == phase,
            CatalogEventAccessGroupRecord.group_id.in_(allowed_groups),
        )
    )


__all__ = [
    "begin_catalog_event",
    "catalog_event_projection",
    "publish_catalog_event",
    "record_catalog_event",
    "snapshot_catalog_event_collection_access_groups",
]

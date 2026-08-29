from __future__ import annotations

from collections.abc import Iterable
from typing import cast

from sqlalchemy import case, exists, false, insert, literal, or_, select, true
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from riverhog_core.app_permissions import ALL_RESOURCES, ApplicationPrincipal
from riverhog_core.catalog_models import (
    CatalogEventRecord,
    CatalogEventTagRecord,
    CollectionTagRecord,
)
from riverhog_core.collection_access import collection_ids, permission_resources, tag_ids


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
    event = CatalogEventRecord(
        change=change,
        collection_id=collection_id,
        occurred_at=occurred_at,
        inventory_identity=inventory_identity,
    )
    session.add(event)
    session.flush()
    for phase, tags in (("before", before_tags), ("after", after_tags)):
        for tag in sorted(set(tags)):
            session.add(
                CatalogEventTagRecord(
                    sequence=event.sequence,
                    phase=phase,
                    tag_id=tag,
                )
            )
    return event


def begin_catalog_event(
    session: Session,
    *,
    change: str,
    collection_id: int,
    occurred_at: str,
    inventory_identity: str,
) -> CatalogEventRecord:
    """Create an event whose tag visibility snapshots will be populated relationally."""

    event = CatalogEventRecord(
        change=change,
        collection_id=collection_id,
        occurred_at=occurred_at,
        inventory_identity=inventory_identity,
    )
    session.add(event)
    session.flush()
    return event


def snapshot_catalog_event_collection_tags(
    session: Session,
    *,
    event: CatalogEventRecord,
    phase: str,
    collection_id: int,
) -> None:
    """Copy one collection's current tag relation without client-side materialization."""

    if phase not in {"before", "after"}:
        raise ValueError("catalog event tag phase is invalid")
    session.execute(
        insert(CatalogEventTagRecord).from_select(
            ("sequence", "phase", "tag_id"),
            select(
                literal(event.sequence),
                literal(phase),
                CollectionTagRecord.tag_id,
            ).where(CollectionTagRecord.collection_id == collection_id),
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
    allowed_tags = tag_ids(resources)
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
            CatalogEventTagRecord.tag_id.in_(allowed_tags),
        )
    )


__all__ = [
    "begin_catalog_event",
    "catalog_event_projection",
    "record_catalog_event",
    "snapshot_catalog_event_collection_tags",
]

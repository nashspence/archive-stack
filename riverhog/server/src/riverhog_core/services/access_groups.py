from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from riverhog_protocol import CollectionAccessGroupStatus
from riverhog_protocol.collection_workflows import canonical_json_sha256
from riverhog_protocol.errors import BadRequest, Conflict, NotFound
from sqlalchemy import asc, desc, select
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ColumnElement
from time_formats import format_utc_timestamp, utc_now

from riverhog_core.app_permissions import ApplicationPrincipal
from riverhog_core.browse import bounded_page, keyset_statement, validate_page_size
from riverhog_core.catalog_db import SessionFactory, make_session_factory, session_scope
from riverhog_core.catalog_events import (
    begin_catalog_event,
    publish_catalog_event,
    snapshot_catalog_event_collection_access_groups,
)
from riverhog_core.catalog_models import (
    CollectionAccessGroupMembershipRecord,
    CollectionAccessGroupRecord,
    CollectionRecord,
)
from riverhog_core.runtime_config import RuntimeConfig

_SORT_FIELDS = frozenset(
    {"id", "display_label", "created_at", "updated_at", "status", "collections"}
)
_SORT_ORDERS = frozenset({"asc", "desc"})


class SqlAlchemyCollectionAccessGroupService:
    """Manage database-only collection authorization groups."""

    def __init__(
        self,
        config: RuntimeConfig,
        *,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._session_factory = session_factory or make_session_factory(config.database_url)

    def create(
        self,
        *,
        idempotency_key: str,
        display_label: str | None,
        creator: ApplicationPrincipal,
    ) -> dict[str, object]:
        key = _idempotency_key(idempotency_key)
        label = _display_label(display_label)
        group_id = canonical_json_sha256(
            {
                "format": "riverhog-collection-access-group-id/v1",
                "created_by_app": creator.app,
                "idempotency_key": key,
            }
        )
        now = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
            existing = session.scalar(
                select(CollectionAccessGroupRecord)
                .where(
                    CollectionAccessGroupRecord.created_by_app == creator.app,
                    CollectionAccessGroupRecord.creation_idempotency_key == key,
                )
                .with_for_update()
            )
            if existing is not None:
                if existing.id != group_id or existing.display_label != label:
                    raise Conflict("collection access group idempotency key was reused")
                return _group_payload(existing)
            record = CollectionAccessGroupRecord(
                id=group_id,
                creation_idempotency_key=key,
                created_by_app=creator.app,
                created_by_key_id=creator.key_id,
                display_label=label,
                status="active",
                authorization_revision=1,
                created_at=now,
                updated_at=now,
                collection_count=0,
            )
            session.add(record)
            session.flush()
            return _group_payload(record)

    def get(self, group_id: str) -> dict[str, object]:
        normalized = _group_id(group_id)
        with session_scope(self._session_factory) as session:
            record = session.get(CollectionAccessGroupRecord, normalized)
            if record is None:
                raise NotFound(f"collection access group not found: {normalized}")
            return _group_payload(record)

    def update(
        self,
        group_id: str,
        *,
        display_label: str | None,
        status: CollectionAccessGroupStatus,
    ) -> dict[str, object]:
        normalized = _group_id(group_id)
        label = _display_label(display_label)
        if status not in {"active", "disabled"}:
            raise BadRequest("collection access group status must be active or disabled")
        with session_scope(self._session_factory) as session:
            record = session.scalar(
                select(CollectionAccessGroupRecord)
                .where(CollectionAccessGroupRecord.id == normalized)
                .with_for_update()
            )
            if record is None:
                raise NotFound(f"collection access group not found: {normalized}")
            if record.display_label == label and record.status == status:
                return _group_payload(record)
            now = format_utc_timestamp(utc_now())
            if record.status != status:
                record.authorization_revision += 1
                record.status = status
            record.display_label = label
            record.updated_at = now
            session.flush()
            return _group_payload(record)

    def list(
        self,
        *,
        page_size: int,
        position: tuple[str | int | bool | bytes | None, ...] | None,
        q: str | None,
        status: CollectionAccessGroupStatus | None,
        sort: str,
        order: str,
    ) -> dict[str, object]:
        validate_page_size(page_size)
        query, statement, key_columns = _group_list_statement(
            q=q,
            status=status,
            sort=sort,
            order=order,
        )
        with session_scope(self._session_factory) as session:
            rows, next_position = bounded_page(
                list(
                    session.execute(
                        keyset_statement(
                            statement,
                            columns=key_columns,
                            position=position,
                            order=order,
                            page_size=page_size,
                        )
                    ).mappings()
                ),
                page_size=page_size,
                position_of=lambda row: _group_list_position(row, sort=sort),
            )
        return {
            "page_size": page_size,
            "_next_position": next_position,
            "query": query,
            "status": status,
            "sort": sort,
            "order": order,
            "groups": [dict(row) for row in rows],
        }

    def iter_groups(
        self,
        *,
        q: str | None,
        status: CollectionAccessGroupStatus | None,
        sort: str,
        order: str,
    ) -> Iterator[dict[str, object]]:
        position: tuple[str | int | bool | bytes | None, ...] | None = None
        while True:
            page = self.list(
                page_size=100,
                position=position,
                q=q,
                status=status,
                sort=sort,
                order=order,
            )
            yield from page["groups"]  # type: ignore[misc]
            position = page["_next_position"]  # type: ignore[assignment]
            if position is None:
                return

    def list_members(
        self,
        group_id: str,
        *,
        page_size: int,
        position: tuple[str | int | bool | bytes | None, ...] | None,
    ) -> dict[str, object]:
        normalized = _group_id(group_id)
        validate_page_size(page_size)
        with session_scope(self._session_factory) as session:
            group = session.get(CollectionAccessGroupRecord, normalized)
            if group is None:
                raise NotFound(f"collection access group not found: {normalized}")
            statement = (
                select(
                    CollectionAccessGroupMembershipRecord.collection_id.label("collection_id"),
                    CollectionAccessGroupMembershipRecord.added_by_app.label("added_by_app"),
                    CollectionAccessGroupMembershipRecord.added_by_key_id.label("added_by_key_id"),
                    CollectionAccessGroupMembershipRecord.added_at.label("added_at"),
                )
                .where(CollectionAccessGroupMembershipRecord.group_id == normalized)
                .order_by(CollectionAccessGroupMembershipRecord.collection_id)
            )
            rows, next_position = bounded_page(
                list(
                    session.execute(
                        keyset_statement(
                            statement,
                            columns=(CollectionAccessGroupMembershipRecord.collection_id,),
                            position=position,
                            order="asc",
                            page_size=page_size,
                        )
                    ).mappings()
                ),
                page_size=page_size,
                position_of=lambda row: (int(row["collection_id"]),),
            )
            authorization_revision = group.authorization_revision
        return {
            "group_id": normalized,
            "authorization_revision": authorization_revision,
            "page_size": page_size,
            "_next_position": next_position,
            "members": [dict(row) for row in rows],
        }

    def list_collection_groups(
        self,
        collection_id: int,
        *,
        page_size: int,
        position: tuple[str | int | bool | bytes | None, ...] | None,
    ) -> dict[str, object]:
        validate_page_size(page_size)
        with session_scope(self._session_factory) as session:
            if session.get(CollectionRecord, collection_id) is None:
                raise NotFound(f"collection not found: {collection_id}")
            statement = (
                select(CollectionAccessGroupRecord)
                .join(
                    CollectionAccessGroupMembershipRecord,
                    CollectionAccessGroupMembershipRecord.group_id
                    == CollectionAccessGroupRecord.id,
                )
                .where(CollectionAccessGroupMembershipRecord.collection_id == collection_id)
                .order_by(CollectionAccessGroupRecord.id)
            )
            rows, next_position = bounded_page(
                list(
                    session.scalars(
                        keyset_statement(
                            statement,
                            columns=(CollectionAccessGroupRecord.id,),
                            position=position,
                            order="asc",
                            page_size=page_size,
                        )
                    )
                ),
                page_size=page_size,
                position_of=lambda row: (row.id,),
            )
        return {
            "collection_id": collection_id,
            "page_size": page_size,
            "_next_position": next_position,
            "groups": [_group_payload(row) for row in rows],
        }

    def add_member(
        self,
        group_id: str,
        collection_id: int,
        *,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        return self._set_membership(
            group_id,
            collection_id,
            present=True,
            principal=principal,
        )

    def remove_member(
        self,
        group_id: str,
        collection_id: int,
        *,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        return self._set_membership(
            group_id,
            collection_id,
            present=False,
            principal=principal,
        )

    def _set_membership(
        self,
        group_id: str,
        collection_id: int,
        *,
        present: bool,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        normalized = _group_id(group_id)
        with session_scope(self._session_factory) as session:
            group = session.scalar(
                select(CollectionAccessGroupRecord)
                .where(CollectionAccessGroupRecord.id == normalized)
                .with_for_update()
            )
            if group is None:
                raise NotFound(f"collection access group not found: {normalized}")
            collection = session.scalar(
                select(CollectionRecord)
                .where(
                    CollectionRecord.id == collection_id,
                    CollectionRecord.is_published.is_(True),
                )
                .with_for_update()
            )
            if collection is None:
                raise NotFound(f"collection not found: {collection_id}")
            membership = session.get(
                CollectionAccessGroupMembershipRecord,
                (collection_id, normalized),
            )
            if (membership is not None) == present:
                return _membership_payload(group, collection_id, present=present, changed=False)
            now = format_utc_timestamp(utc_now())
            event = begin_catalog_event(
                session,
                change="updated",
                collection_id=collection_id,
                occurred_at=now,
                inventory_identity=collection.inventory_identity,
            )
            snapshot_catalog_event_collection_access_groups(
                session,
                event=event,
                phase="before",
                collection_id=collection_id,
            )
            if present:
                session.add(
                    CollectionAccessGroupMembershipRecord(
                        collection_id=collection_id,
                        group_id=normalized,
                        added_by_app=principal.app,
                        added_by_key_id=principal.key_id,
                        added_at=now,
                    )
                )
                group.collection_count += 1
            else:
                assert membership is not None
                session.delete(membership)
                group.collection_count -= 1
            group.authorization_revision += 1
            group.updated_at = now
            session.flush()
            snapshot_catalog_event_collection_access_groups(
                session,
                event=event,
                phase="after",
                collection_id=collection_id,
            )
            publish_catalog_event(session, event=event)
            session.flush()
            return _membership_payload(group, collection_id, present=present, changed=True)


def _group_list_statement(
    *,
    q: str | None,
    status: CollectionAccessGroupStatus | None,
    sort: str,
    order: str,
) -> tuple[str | None, Select[Any], tuple[Any, ...]]:
    if sort not in _SORT_FIELDS:
        raise BadRequest(f"sort must be one of {', '.join(sorted(_SORT_FIELDS))}")
    if order not in _SORT_ORDERS:
        raise BadRequest("order must be asc or desc")
    if status is not None and status not in {"active", "disabled"}:
        raise BadRequest("collection access group status must be active or disabled")
    query = q.strip().casefold() if q is not None and q.strip() else None
    filters: list[ColumnElement[bool]] = []
    if query is not None:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        filters.append(
            CollectionAccessGroupRecord.search_text.like(
                f"%{escaped}%",
                escape="\\",
            )
        )
    if status is not None:
        filters.append(CollectionAccessGroupRecord.status == status)
    sort_columns = {
        "id": (CollectionAccessGroupRecord.id,),
        "display_label": (
            CollectionAccessGroupRecord.display_label_sort,
            CollectionAccessGroupRecord.id,
        ),
        "created_at": (CollectionAccessGroupRecord.created_at, CollectionAccessGroupRecord.id),
        "updated_at": (CollectionAccessGroupRecord.updated_at, CollectionAccessGroupRecord.id),
        "status": (CollectionAccessGroupRecord.status, CollectionAccessGroupRecord.id),
        "collections": (
            CollectionAccessGroupRecord.collection_count,
            CollectionAccessGroupRecord.id,
        ),
    }
    columns = sort_columns[sort]
    ordering = asc if order == "asc" else desc
    statement = select(
        CollectionAccessGroupRecord.id.label("id"),
        CollectionAccessGroupRecord.display_label.label("display_label"),
        CollectionAccessGroupRecord.status.label("status"),
        CollectionAccessGroupRecord.authorization_revision.label("authorization_revision"),
        CollectionAccessGroupRecord.collection_count.label("collection_count"),
        CollectionAccessGroupRecord.created_by_app.label("created_by_app"),
        CollectionAccessGroupRecord.created_by_key_id.label("created_by_key_id"),
        CollectionAccessGroupRecord.created_at.label("created_at"),
        CollectionAccessGroupRecord.updated_at.label("updated_at"),
    ).where(*filters)
    return query, statement.order_by(*(ordering(column) for column in columns)), columns


def _group_list_position(row: Any, *, sort: str) -> tuple[str | int, ...]:
    if sort == "id":
        return (str(row["id"]),)
    if sort == "display_label":
        return (str(row["display_label"] or ""), str(row["id"]))
    if sort in {"created_at", "updated_at", "status"}:
        return (str(row[sort]), str(row["id"]))
    return (int(row["collection_count"]), str(row["id"]))


def _group_payload(record: CollectionAccessGroupRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "display_label": record.display_label,
        "status": record.status,
        "authorization_revision": record.authorization_revision,
        "collection_count": record.collection_count,
        "created_by_app": record.created_by_app,
        "created_by_key_id": record.created_by_key_id,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _membership_payload(
    group: CollectionAccessGroupRecord,
    collection_id: int,
    *,
    present: bool,
    changed: bool,
) -> dict[str, object]:
    return {
        "group_id": group.id,
        "collection_id": collection_id,
        "present": present,
        "changed": changed,
        "authorization_revision": group.authorization_revision,
        "collection_count": group.collection_count,
    }


def _group_id(value: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or value != value.casefold():
        raise BadRequest("collection access group id must be lowercase SHA-256")
    try:
        int(value, 16)
    except ValueError as exc:
        raise BadRequest("collection access group id must be lowercase SHA-256") from exc
    return value


def _idempotency_key(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 300 or value != value.strip():
        raise BadRequest("collection access group idempotency key must be 1 to 300 characters")
    return value


def _display_label(value: str | None) -> str | None:
    if value is None:
        return None
    if not value or len(value) > 300 or value != value.strip():
        raise BadRequest("collection access group display label must be 1 to 300 characters")
    return value


__all__ = ["SqlAlchemyCollectionAccessGroupService"]

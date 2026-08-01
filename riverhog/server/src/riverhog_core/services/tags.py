from __future__ import annotations

import math
import secrets
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Literal

from riverhog_protocol.errors import BadRequest, Conflict, NotFound
from riverhog_protocol.paths import PathNormalizationError, normalize_tag
from sqlalchemy import asc, desc, exists, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ColumnElement
from time_formats import format_utc_timestamp, utc_now

from riverhog_core.app_permissions import (
    CATALOG_READ,
    COLLECTION_TAGS_MANAGE,
    COLLECTIONS_CREATE,
    ApplicationAccess,
    ApplicationPrincipal,
    tag_resource,
)
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_events import record_catalog_event
from riverhog_core.catalog_models import (
    AppKeyAccessGrantRecord,
    AppKeyRecord,
    CatalogEventRecord,
    CatalogEventTagRecord,
    CollectionArchiveCopyRecord,
    CollectionFileRecord,
    CollectionMetadataPublicationRecord,
    CollectionRecord,
    CollectionTagRecord,
    CollectionUploadRecord,
    CollectionUploadTagRecord,
    TagRecord,
)
from riverhog_core.collection_access import require_collection_access, tag_access_filter
from riverhog_core.collection_metadata import collection_record_manifest
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.app_keys import active_key_filter
from riverhog_core.services.collection_mutations import require_collection_mutation_allowed
from riverhog_core.services.lifecycle_events import (
    SqlAlchemyLifecycleEventService,
    event_context_json,
)
from riverhog_core.services.operation_plans import (
    PLAN_TTL,
    challenge_expiry,
    challenge_has_shape,
    plan_challenge,
)

_SORT_FIELDS = {"id", "created_at", "collections"}
_DELETE_CHALLENGE_PREFIX = "delete-tag"
_DEPENDENCY_SAMPLE_LIMIT = 10
_TAG_DELETE_WARNING = (
    "Deleting this empty tag removes only its Riverhog catalog definition. "
    "Riverhog can inspect only dependencies recorded in its own catalog. Confirm that "
    "clients, companions, and automation no longer reference this tag."
)


def canonical_tag(value: str) -> str:
    try:
        normalized = normalize_tag(value)
    except PathNormalizationError as exc:
        raise BadRequest(str(exc)) from exc
    if value != normalized:
        raise BadRequest("tag id must be canonical")
    return normalized


class SqlAlchemyTagService:
    def __init__(self, config: RuntimeConfig) -> None:
        self._session_factory = make_session_factory(config.database_url)
        self._lifecycle_events = SqlAlchemyLifecycleEventService(config)

    def create(
        self,
        tag: str,
        *,
        creator: ApplicationPrincipal,
    ) -> dict[str, object]:
        normalized = canonical_tag(tag)
        now = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
            if session.get(TagRecord, normalized) is not None:
                raise Conflict(f"tag already exists: {normalized}")
            record = TagRecord(
                id=normalized,
                created_by_app=creator.app,
                created_by_key_id=creator.key_id,
                created_at=now,
            )
            session.add(record)
            session.flush()
            create_access = ApplicationAccess(COLLECTIONS_CREATE, tag_resource(normalized))
            if creator.key_id is not None and not creator.allows(
                create_access.permission,
                create_access.resource,
            ):
                session.add(
                    AppKeyAccessGrantRecord(
                        key_id=creator.key_id,
                        permission=create_access.permission,
                        resource=create_access.resource,
                        created_at=now,
                    )
                )
            return _tag_payload(record, collections=0)

    def get(
        self,
        tag: str,
        *,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        normalized = canonical_tag(tag)
        with session_scope(self._session_factory) as session:
            permitted = session.scalar(
                select(TagRecord.id)
                .where(TagRecord.id == normalized)
                .where(tag_access_filter(TagRecord.id, principal, CATALOG_READ))
            )
            if permitted is None:
                raise NotFound(f"tag not found: {normalized}")
            record = session.get(TagRecord, normalized)
            assert record is not None
            collections = int(
                session.scalar(
                    select(func.count())
                    .select_from(CollectionTagRecord)
                    .where(CollectionTagRecord.tag_id == normalized)
                )
                or 0
            )
            return _tag_payload(record, collections=collections)

    def plan_deletion(self, tag: str) -> dict[str, object]:
        normalized = canonical_tag(tag)
        expires = (utc_now() + PLAN_TTL).replace(microsecond=0)
        with session_scope(self._session_factory) as session:
            if session.get(TagRecord, normalized) is None:
                raise NotFound(f"tag not found: {normalized}")
            plan = _tag_deletion_plan(session, tag=normalized, expires_at=expires)
            plan["challenge"] = (
                None
                if plan["blockers"]
                else plan_challenge(_DELETE_CHALLENGE_PREFIX, plan, expires)
            )
            return plan

    def delete(self, tag: str, *, challenge: str) -> dict[str, object]:
        normalized = canonical_tag(tag)
        supplied = challenge.strip()
        if not supplied:
            raise BadRequest("tag deletion challenge is required")
        with session_scope(self._session_factory) as session:
            record = session.scalar(
                select(TagRecord).where(TagRecord.id == normalized).with_for_update()
            )
            if record is None:
                if not challenge_has_shape(supplied, prefix=_DELETE_CHALLENGE_PREFIX):
                    raise NotFound(f"tag not found: {normalized}")
                return {"status": "already_absent", "tag": normalized}
            expires = challenge_expiry(
                supplied,
                prefix=_DELETE_CHALLENGE_PREFIX,
                operation="tag deletion",
            )
            if utc_now() > expires:
                raise Conflict("tag deletion plan has expired; request a new plan")
            plan = _tag_deletion_plan(session, tag=normalized, expires_at=expires)
            expected = plan_challenge(_DELETE_CHALLENGE_PREFIX, plan, expires)
            if not secrets.compare_digest(expected, supplied):
                raise Conflict("tag deletion plan changed; request a new plan")
            blockers = plan["blockers"]
            if isinstance(blockers, list) and blockers:
                raise Conflict(f"tag deletion is blocked: {blockers[0]}")
            session.delete(record)
        return {"status": "deleted", "tag": normalized}

    def list(
        self,
        *,
        page: int,
        per_page: int,
        q: str | None,
        sort: str,
        order: str,
        all_items: bool,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        if page < 1:
            raise BadRequest("page must be greater than or equal to 1")
        if per_page < 1 or per_page > 100:
            raise BadRequest("per_page must be between 1 and 100")
        if sort not in _SORT_FIELDS:
            raise BadRequest(f"sort must be one of {', '.join(sorted(_SORT_FIELDS))}")
        if order not in {"asc", "desc"}:
            raise BadRequest("order must be asc or desc")
        query = q.strip() if q is not None else None
        collection_counts = (
            select(
                CollectionTagRecord.tag_id,
                func.count().label("collections"),
            )
            .group_by(CollectionTagRecord.tag_id)
            .subquery()
        )
        collections = func.coalesce(collection_counts.c.collections, 0)
        filters: list[ColumnElement[bool]] = [
            tag_access_filter(TagRecord.id, principal, CATALOG_READ)
        ]
        if query:
            filters.append(
                func.lower(TagRecord.id).like(
                    _like_pattern(query.casefold()),
                    escape="\\",
                )
            )
        base = (
            select(
                TagRecord.id.label("id"),
                TagRecord.created_by_app.label("created_by_app"),
                TagRecord.created_by_key_id.label("created_by_key_id"),
                TagRecord.created_at.label("created_at"),
                collections.label("collections"),
            )
            .outerjoin(collection_counts, collection_counts.c.tag_id == TagRecord.id)
            .where(*filters)
        )
        columns = {
            "id": TagRecord.id,
            "created_at": TagRecord.created_at,
            "collections": collections,
        }
        direction = desc if order == "desc" else asc
        statement = base.order_by(direction(columns[sort]), asc(TagRecord.id))
        with session_scope(self._session_factory) as session:
            total = int(session.scalar(select(func.count()).select_from(base.subquery())) or 0)
            if not all_items:
                statement = statement.offset((page - 1) * per_page).limit(per_page)
            rows = [dict(row) for row in session.execute(statement).mappings().all()]
        return {
            "page": 1 if all_items else page,
            "per_page": total if all_items else per_page,
            "total": total,
            "pages": (
                (1 if total else 0) if all_items else math.ceil(total / per_page) if total else 0
            ),
            "sort": sort,
            "order": order,
            "query": query,
            "tags": rows,
        }

    def get_collection(
        self,
        collection_id: int,
        *,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            collection = session.get(CollectionRecord, collection_id)
            if collection is None:
                raise NotFound(f"collection not found: {collection_id}")
            require_collection_access(session, principal, CATALOG_READ, collection_id)
            return _collection_tags_payload(session, collection)

    def replace_collection(
        self,
        collection_id: int,
        tags: Sequence[str],
        *,
        principal: ApplicationPrincipal,
        event_context: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        normalized_tags = tuple(sorted({canonical_tag(tag) for tag in tags}))
        if len(normalized_tags) != len(tags):
            raise BadRequest("collection tags must not contain duplicates")
        return self._mutate_collection_tags(
            collection_id,
            principal=principal,
            event_context=event_context,
            mutation="replace",
            tag=None,
            replacement=normalized_tags,
        )

    def add_collection_tag(
        self,
        collection_id: int,
        tag: str,
        *,
        principal: ApplicationPrincipal,
        event_context: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        return self._mutate_collection_tags(
            collection_id,
            principal=principal,
            event_context=event_context,
            mutation="add",
            tag=canonical_tag(tag),
            replacement=None,
        )

    def remove_collection_tag(
        self,
        collection_id: int,
        tag: str,
        *,
        principal: ApplicationPrincipal,
        event_context: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        return self._mutate_collection_tags(
            collection_id,
            principal=principal,
            event_context=event_context,
            mutation="remove",
            tag=canonical_tag(tag),
            replacement=None,
        )

    def _mutate_collection_tags(
        self,
        collection_id: int,
        *,
        principal: ApplicationPrincipal,
        event_context: Mapping[str, object] | None,
        mutation: Literal["replace", "add", "remove"],
        tag: str | None,
        replacement: tuple[str, ...] | None,
    ) -> dict[str, object]:
        normalized_context_json = event_context_json(event_context)
        with session_scope(self._session_factory) as session:
            collection = session.scalar(
                select(CollectionRecord)
                .where(CollectionRecord.id == collection_id)
                .with_for_update()
            )
            if collection is None:
                raise NotFound(f"collection not found: {collection_id}")
            require_collection_access(
                session,
                principal,
                COLLECTION_TAGS_MANAGE,
                collection_id,
            )
            require_collection_mutation_allowed(session, collection_id)
            current_tags = tuple(
                session.scalars(
                    select(CollectionTagRecord.tag_id)
                    .where(CollectionTagRecord.collection_id == collection_id)
                    .order_by(CollectionTagRecord.tag_id)
                ).all()
            )
            if mutation == "replace":
                assert replacement is not None
                normalized_tags = replacement
            elif mutation == "add":
                assert tag is not None
                if tag in current_tags:
                    raise Conflict(f"collection already has tag: {tag}")
                normalized_tags = tuple(sorted((*current_tags, tag)))
            else:
                assert tag is not None
                if tag not in current_tags:
                    raise NotFound(f"collection tag not found: {tag}")
                normalized_tags = tuple(current for current in current_tags if current != tag)
            _require_tags(session, normalized_tags)
            if current_tags == normalized_tags:
                return _collection_tags_payload(session, collection)

            session.query(CollectionTagRecord).filter(
                CollectionTagRecord.collection_id == collection_id
            ).delete(synchronize_session=False)
            now = format_utc_timestamp(utc_now())
            for tag in normalized_tags:
                session.add(
                    CollectionTagRecord(
                        collection_id=collection_id,
                        tag_id=tag,
                        assigned_by_app=principal.app,
                        assigned_by_key_id=principal.key_id,
                        assigned_at=now,
                    )
                )
            collection.metadata_revision += 1
            collection.metadata_updated_at = now
            files = list(
                session.execute(
                    select(
                        CollectionFileRecord.path,
                        CollectionFileRecord.bytes,
                        CollectionFileRecord.sha256,
                    )
                    .where(CollectionFileRecord.collection_id == collection_id)
                    .order_by(CollectionFileRecord.path)
                ).tuples()
            )
            _, collection.record_etag = collection_record_manifest(
                collection_id=collection_id,
                content_etag=collection.content_etag,
                metadata_revision=collection.metadata_revision,
                tags=normalized_tags,
                files=files,
            )
            _schedule_metadata_publications(session, collection)
            record_catalog_event(
                session,
                change="updated",
                collection_id=collection_id,
                occurred_at=now,
                record_etag=collection.record_etag,
                before_tags=current_tags,
                after_tags=normalized_tags,
            )
            session.flush()
            self._lifecycle_events.emit_collection(
                type="collection.tags_changed",
                collection_id=collection_id,
                terminal=True,
                initiator=principal,
                event_context_json=normalized_context_json,
                session=session,
            )
            return _collection_tags_payload(session, collection)


def _tag_deletion_plan(
    session: Session,
    *,
    tag: str,
    expires_at: datetime,
) -> dict[str, object]:
    collection_query = (
        select(CollectionTagRecord.collection_id)
        .where(CollectionTagRecord.tag_id == tag)
        .order_by(CollectionTagRecord.collection_id)
    )
    collections = _dependency_summary(
        session,
        collection_query,
        render=lambda row: str(row[0]),
    )

    upload_query = (
        select(CollectionUploadTagRecord.collection_id)
        .join(
            CollectionUploadRecord,
            CollectionUploadRecord.collection_id == CollectionUploadTagRecord.collection_id,
        )
        .where(
            CollectionUploadTagRecord.tag_id == tag,
            or_(
                CollectionUploadRecord.state.is_(None),
                CollectionUploadRecord.state.notin_(("canceled", "expired")),
            ),
        )
        .order_by(CollectionUploadTagRecord.collection_id)
    )
    uploads = _dependency_summary(
        session,
        upload_query,
        render=lambda row: str(row[0]),
    )

    now = format_utc_timestamp(utc_now())
    access_query = (
        select(
            AppKeyRecord.app,
            AppKeyRecord.id,
            AppKeyAccessGrantRecord.permission,
        )
        .join(AppKeyRecord, AppKeyRecord.id == AppKeyAccessGrantRecord.key_id)
        .where(
            AppKeyAccessGrantRecord.resource == tag_resource(tag),
            active_key_filter(now),
        )
        .order_by(AppKeyRecord.app, AppKeyRecord.id, AppKeyAccessGrantRecord.permission)
    )
    access = _dependency_summary(
        session,
        access_query,
        render=lambda row: f"{row[0]}/{row[1]}/{row[2]}",
    )

    before_tag = CatalogEventTagRecord.__table__.alias("removed_tag_before")
    after_tag = CatalogEventTagRecord.__table__.alias("removed_tag_after")
    removals = (
        select(
            CatalogEventRecord.collection_id.label("collection_id"),
            func.max(CatalogEventRecord.occurred_at).label("removed_at"),
        )
        .where(
            exists(
                select(1).where(
                    before_tag.c.sequence == CatalogEventRecord.sequence,
                    before_tag.c.phase == "before",
                    before_tag.c.tag_id == tag,
                )
            ),
            ~exists(
                select(1).where(
                    after_tag.c.sequence == CatalogEventRecord.sequence,
                    after_tag.c.phase == "after",
                    after_tag.c.tag_id == tag,
                )
            ),
        )
        .group_by(CatalogEventRecord.collection_id)
        .subquery()
    )
    publication_query = (
        select(
            CollectionMetadataPublicationRecord.collection_id,
            CollectionMetadataPublicationRecord.store,
        )
        .join(
            removals,
            removals.c.collection_id == CollectionMetadataPublicationRecord.collection_id,
        )
        .where(
            or_(
                CollectionMetadataPublicationRecord.published_at.is_(None),
                CollectionMetadataPublicationRecord.published_at < removals.c.removed_at,
            ),
            or_(
                CollectionMetadataPublicationRecord.state != "published",
                CollectionMetadataPublicationRecord.published_revision.is_(None),
                CollectionMetadataPublicationRecord.published_revision
                < CollectionMetadataPublicationRecord.desired_revision,
            ),
        )
        .order_by(
            CollectionMetadataPublicationRecord.collection_id,
            CollectionMetadataPublicationRecord.store,
        )
    )
    publications = _dependency_summary(
        session,
        publication_query,
        render=lambda row: f"{row[0]}/{row[1]}",
    )

    blockers: list[str] = []
    if collections["count"]:
        blockers.append(
            f"{collections['count']} collection(s) still use this tag; "
            f"run riverhog collection list --tag {tag}"
        )
    if uploads["count"]:
        blockers.append(
            f"{uploads['count']} upload session(s) still use this tag; "
            f"run riverhog collection upload list --tag {tag}"
        )
    if access["count"]:
        blockers.append(
            f"{access['count']} active app-key access binding(s) still use this tag; "
            "run riverhog app key access list "
            f"--resource {tag_resource(tag)} --active"
        )
    if publications["count"]:
        blockers.append(
            f"{publications['count']} collection metadata publication(s) are still pending "
            "after tag removal; wait for publication and request a new plan"
        )
    return {
        "status": "blocked" if blockers else "ready",
        "tag": tag,
        "warning": _TAG_DELETE_WARNING,
        "expires_at": format_utc_timestamp(expires_at),
        "challenge": None,
        "dependencies": {
            "collections": collections,
            "upload_sessions": uploads,
            "app_key_access": access,
            "metadata_publications": publications,
        },
        "blockers": blockers,
    }


def _dependency_summary(
    session: Session,
    query: Select[Any],
    *,
    render: Callable[[Any], str],
) -> dict[str, object]:
    total = int(
        session.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    )
    rows = session.execute(query.limit(_DEPENDENCY_SAMPLE_LIMIT)).all()
    return {
        "count": total,
        "sample": [render(row) for row in rows],
        "truncated": total > _DEPENDENCY_SAMPLE_LIMIT,
    }


def _tag_payload(record: TagRecord, *, collections: int) -> dict[str, object]:
    return {
        "id": record.id,
        "created_by_app": record.created_by_app,
        "created_by_key_id": record.created_by_key_id,
        "created_at": record.created_at,
        "collections": collections,
    }


def _collection_tags_payload(
    session: Session,
    collection: CollectionRecord,
) -> dict[str, object]:
    tags = session.scalars(
        select(CollectionTagRecord.tag_id)
        .where(CollectionTagRecord.collection_id == collection.id)
        .order_by(CollectionTagRecord.tag_id)
    ).all()
    return {
        "collection_id": collection.id,
        "metadata_revision": collection.metadata_revision,
        "record_etag": collection.record_etag,
        "tags": list(tags),
    }


def _require_tags(session: Session, tags: Sequence[str]) -> None:
    if not tags:
        return
    existing = set(
        session.scalars(select(TagRecord.id).where(TagRecord.id.in_(tags)).with_for_update()).all()
    )
    missing = sorted(set(tags) - existing)
    if missing:
        raise NotFound(f"tag not found: {missing[0]}")


def _schedule_metadata_publications(session: Session, collection: CollectionRecord) -> None:
    copies = session.scalars(
        select(CollectionArchiveCopyRecord).where(
            CollectionArchiveCopyRecord.collection_id == collection.id,
            CollectionArchiveCopyRecord.archive_storage_prefix.is_not(None),
        )
    ).all()
    for copy in copies:
        publication = session.get(
            CollectionMetadataPublicationRecord,
            (collection.id, copy.store),
        )
        if publication is None:
            session.add(
                CollectionMetadataPublicationRecord(
                    collection_id=collection.id,
                    store=copy.store,
                    desired_revision=collection.metadata_revision,
                    state="pending",
                    attempt_count=0,
                    next_attempt_at=collection.metadata_updated_at,
                )
            )
        else:
            publication.desired_revision = collection.metadata_revision
            publication.state = "pending"
            publication.attempt_count = 0
            publication.next_attempt_at = collection.metadata_updated_at
            publication.failure = None


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"

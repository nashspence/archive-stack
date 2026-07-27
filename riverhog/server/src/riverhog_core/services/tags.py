from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from riverhog_protocol.errors import BadRequest, Conflict, NotFound
from riverhog_protocol.paths import PathNormalizationError, normalize_tag
from sqlalchemy import asc, desc, func, select
from sqlalchemy.orm import Session
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
from riverhog_core.catalog_models import (
    AppKeyAccessGrantRecord,
    CatalogEventRecord,
    CollectionArchiveCopyRecord,
    CollectionFileRecord,
    CollectionMetadataPublicationRecord,
    CollectionRecord,
    CollectionTagRecord,
    TagRecord,
)
from riverhog_core.collection_access import require_collection_access, tag_access_filter
from riverhog_core.collection_metadata import collection_record_manifest
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.collection_custody import require_collection_mutation_allowed
from riverhog_core.services.lifecycle_events import (
    SqlAlchemyLifecycleEventService,
    event_context_json,
)

_SORT_FIELDS = {"id", "created_at", "collections"}


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
            _require_tags(session, normalized_tags)
            current_tags = tuple(
                session.scalars(
                    select(CollectionTagRecord.tag_id)
                    .where(CollectionTagRecord.collection_id == collection_id)
                    .order_by(CollectionTagRecord.tag_id)
                ).all()
            )
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
            session.add(
                CatalogEventRecord(
                    change="updated",
                    collection_id=collection_id,
                    occurred_at=now,
                    record_etag=collection.record_etag,
                )
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
    existing = set(session.scalars(select(TagRecord.id).where(TagRecord.id.in_(tags))).all())
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

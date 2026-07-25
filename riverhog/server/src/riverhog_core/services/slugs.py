from __future__ import annotations

import math

from riverhog_protocol.errors import BadRequest, Conflict, NotFound
from riverhog_protocol.paths import PathNormalizationError, normalize_upload_slug
from sqlalchemy import asc, desc, func, select
from sqlalchemy.sql.elements import ColumnElement
from time_formats import format_utc_timestamp, utc_now

from riverhog_core.app_permissions import (
    CATALOG_READ,
    COLLECTIONS_UPLOAD,
    ApplicationAccess,
    ApplicationPrincipal,
    slug_resource,
)
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    AppKeyAccessGrantRecord,
    CollectionRecord,
    CollectionSlugRecord,
    CollectionUploadRecord,
)
from riverhog_core.collection_access import require_slug_access, slug_access_filter
from riverhog_core.runtime_config import RuntimeConfig

_SORT_FIELDS = {"id", "created_at", "collections", "uploads"}


def normalize_slug(value: str) -> str:
    try:
        normalized = normalize_upload_slug(value)
    except PathNormalizationError as exc:
        raise BadRequest(str(exc)) from exc
    if value != normalized:
        raise BadRequest("slug id must be canonical")
    return normalized


class SqlAlchemySlugService:
    def __init__(self, config: RuntimeConfig) -> None:
        self._session_factory = make_session_factory(config.database_url)

    def create(
        self,
        slug: str,
        *,
        creator: ApplicationPrincipal,
    ) -> dict[str, object]:
        normalized = normalize_slug(slug)
        now = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
            if session.get(CollectionSlugRecord, normalized) is not None:
                raise Conflict(f"collection slug already exists: {normalized}")
            record = CollectionSlugRecord(
                id=normalized,
                created_by_app=creator.app,
                created_by_key_id=creator.key_id,
                created_at=now,
            )
            session.add(record)
            session.flush()
            upload_access = ApplicationAccess(COLLECTIONS_UPLOAD, slug_resource(normalized))
            if creator.key_id is not None and not creator.allows(
                upload_access.permission, upload_access.resource
            ):
                session.add(
                    AppKeyAccessGrantRecord(
                        key_id=creator.key_id,
                        permission=upload_access.permission,
                        resource=upload_access.resource,
                        created_at=now,
                    )
                )
            return _slug_payload(record, collections=0, uploads=0)

    def get(
        self,
        slug: str,
        *,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        normalized = normalize_slug(slug)
        require_slug_access(principal, CATALOG_READ, normalized)
        with session_scope(self._session_factory) as session:
            record = session.get(CollectionSlugRecord, normalized)
            if record is None:
                raise NotFound(f"collection slug not found: {normalized}")
            collections = int(
                session.scalar(
                    select(func.count()).select_from(CollectionRecord).where(
                        CollectionRecord.slug == normalized
                    )
                )
                or 0
            )
            uploads = int(
                session.scalar(
                    select(func.count()).select_from(CollectionUploadRecord).where(
                        CollectionUploadRecord.slug == normalized
                    )
                )
                or 0
            )
            return _slug_payload(record, collections=collections, uploads=uploads)

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
            select(CollectionRecord.slug, func.count().label("collections"))
            .group_by(CollectionRecord.slug)
            .subquery()
        )
        upload_counts = (
            select(CollectionUploadRecord.slug, func.count().label("uploads"))
            .group_by(CollectionUploadRecord.slug)
            .subquery()
        )
        collections = func.coalesce(collection_counts.c.collections, 0)
        uploads = func.coalesce(upload_counts.c.uploads, 0)
        filters: list[ColumnElement[bool]] = [
            slug_access_filter(CollectionSlugRecord.id, principal, CATALOG_READ)
        ]
        if query:
            filters.append(
                func.lower(CollectionSlugRecord.id).like(
                    _like_pattern(query.casefold()),
                    escape="\\",
                )
            )
        base = (
            select(
                CollectionSlugRecord.id.label("id"),
                CollectionSlugRecord.created_by_app.label("created_by_app"),
                CollectionSlugRecord.created_by_key_id.label("created_by_key_id"),
                CollectionSlugRecord.created_at.label("created_at"),
                collections.label("collections"),
                uploads.label("uploads"),
            )
            .outerjoin(collection_counts, collection_counts.c.slug == CollectionSlugRecord.id)
            .outerjoin(upload_counts, upload_counts.c.slug == CollectionSlugRecord.id)
            .where(*filters)
        )
        columns = {
            "id": CollectionSlugRecord.id,
            "created_at": CollectionSlugRecord.created_at,
            "collections": collections,
            "uploads": uploads,
        }
        direction = desc if order == "desc" else asc
        statement = base.order_by(direction(columns[sort]), asc(CollectionSlugRecord.id))
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
                (1 if total else 0)
                if all_items
                else math.ceil(total / per_page)
                if total
                else 0
            ),
            "sort": sort,
            "order": order,
            "query": query,
            "slugs": rows,
        }


def _slug_payload(
    record: CollectionSlugRecord,
    *,
    collections: int,
    uploads: int,
) -> dict[str, object]:
    return {
        "id": record.id,
        "created_by_app": record.created_by_app,
        "created_by_key_id": record.created_by_key_id,
        "created_at": record.created_at,
        "collections": collections,
        "uploads": uploads,
    }


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"

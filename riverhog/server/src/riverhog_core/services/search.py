from __future__ import annotations

import math
from collections.abc import Iterator
from typing import Any

from riverhog_protocol.errors import BadRequest
from riverhog_protocol.paths import PathNormalizationError, normalize_collection_id
from sqlalchemy import String, asc, cast, desc, func, literal, select
from sqlalchemy.sql.elements import ColumnElement
from state_schema import read_snapshot

from riverhog_core.app_permissions import CATALOG_READ, ApplicationPrincipal
from riverhog_core.artifact_access import artifact_scope_filter
from riverhog_core.catalog_db import SessionFactory, make_session_factory
from riverhog_core.catalog_models import CollectionFileRecord
from riverhog_core.collection_access import collection_access_filter, require_collection_access
from riverhog_core.runtime_config import RuntimeConfig

_SORT_FIELDS = {"file_ref", "collection_id", "path", "bytes"}


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _order_expressions(
    sort: str,
    order: str,
) -> tuple[ColumnElement[Any], ...]:
    direction = desc if order == "desc" else asc
    if sort == "file_ref":
        return (
            direction(CollectionFileRecord.collection_id),
            direction(CollectionFileRecord.path),
        )
    if sort == "collection_id":
        return (
            direction(CollectionFileRecord.collection_id),
            asc(CollectionFileRecord.path),
        )
    if sort == "path":
        return (
            direction(CollectionFileRecord.path),
            asc(CollectionFileRecord.collection_id),
        )
    if sort == "bytes":
        return (
            direction(CollectionFileRecord.bytes),
            asc(CollectionFileRecord.collection_id),
            asc(CollectionFileRecord.path),
        )
    raise BadRequest(f"sort must be one of {', '.join(sorted(_SORT_FIELDS))}")


class SqlAlchemySearchService:
    def __init__(
        self,
        config: RuntimeConfig,
        *,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._session_factory = session_factory or make_session_factory(config.database_url)

    def search(
        self,
        *,
        q: str | None,
        page: int,
        per_page: int,
        sort: str,
        order: str,
        collection: int | None = None,
        principal: ApplicationPrincipal | None = None,
    ) -> dict[str, object]:
        if page < 1:
            raise BadRequest("page must be greater than or equal to 1")
        if per_page < 1 or per_page > 100:
            raise BadRequest("per_page must be between 1 and 100")
        if sort not in _SORT_FIELDS:
            raise BadRequest(f"sort must be one of {', '.join(sorted(_SORT_FIELDS))}")
        if order not in {"asc", "desc"}:
            raise BadRequest("order must be asc or desc")

        normalized_collection, query, filters = _search_filters(
            q=q, collection=collection, principal=principal
        )
        with read_snapshot(self._session_factory) as session:
            if normalized_collection is not None:
                require_collection_access(
                    session,
                    principal,
                    CATALOG_READ,
                    normalized_collection,
                )
            total = session.scalar(
                select(func.count()).select_from(CollectionFileRecord).where(*filters)
            )
            total_count = int(total or 0)
            stmt = (
                select(
                    CollectionFileRecord.collection_id,
                    CollectionFileRecord.path,
                    CollectionFileRecord.bytes,
                    CollectionFileRecord.sha256,
                )
                .where(*filters)
                .order_by(*_order_expressions(sort, order))
            )
            stmt = stmt.offset((page - 1) * per_page).limit(per_page)
            rows = session.execute(stmt).all()

        return {
            "query": query,
            "collection": normalized_collection,
            "page": page,
            "per_page": per_page,
            "total": total_count,
            "pages": math.ceil(total_count / per_page) if total_count else 0,
            "sort": sort,
            "order": order,
            "files": [
                {
                    "file_ref": f"{row.collection_id}/{row.path}",
                    "collection_id": row.collection_id,
                    "path": row.path,
                    "bytes": row.bytes,
                    "sha256": row.sha256,
                }
                for row in rows
            ],
        }

    def iter_files(
        self,
        *,
        q: str | None,
        sort: str,
        order: str,
        collection: int | None = None,
        principal: ApplicationPrincipal | None = None,
    ) -> Iterator[dict[str, object]]:
        if sort not in _SORT_FIELDS:
            raise BadRequest(f"sort must be one of {', '.join(sorted(_SORT_FIELDS))}")
        if order not in {"asc", "desc"}:
            raise BadRequest("order must be asc or desc")
        normalized_collection, _query, filters = _search_filters(
            q=q, collection=collection, principal=principal
        )
        statement = (
            select(
                CollectionFileRecord.collection_id,
                CollectionFileRecord.path,
                CollectionFileRecord.bytes,
                CollectionFileRecord.sha256,
            )
            .where(*filters)
            .order_by(*_order_expressions(sort, order))
            .execution_options(yield_per=100)
        )
        with read_snapshot(self._session_factory) as session:
            if normalized_collection is not None:
                require_collection_access(session, principal, CATALOG_READ, normalized_collection)
            for row in session.execute(statement):
                yield {
                    "file_ref": f"{row.collection_id}/{row.path}",
                    "collection_id": row.collection_id,
                    "path": row.path,
                    "bytes": row.bytes,
                    "sha256": row.sha256,
                }


def _search_filters(
    *,
    q: str | None,
    collection: int | None,
    principal: ApplicationPrincipal | None,
) -> tuple[int | None, str | None, list[ColumnElement[bool]]]:
    normalized_collection: int | None = None
    if collection:
        try:
            normalized_collection = normalize_collection_id(collection)
        except PathNormalizationError as exc:
            raise BadRequest(str(exc)) from exc
    file_ref_expr = (
        cast(CollectionFileRecord.collection_id, String) + literal("/") + CollectionFileRecord.path
    )
    filters: list[ColumnElement[bool]] = [
        collection_access_filter(CollectionFileRecord.collection_id, principal, CATALOG_READ)
    ]
    filters.append(
        artifact_scope_filter(
            CollectionFileRecord.collection_id,
            CollectionFileRecord.path,
            principal,
        )
    )
    query = q.strip() if q is not None else None
    if query:
        filters.append(func.lower(file_ref_expr).like(_like_pattern(query.casefold()), escape="\\"))
    if normalized_collection is not None:
        filters.append(CollectionFileRecord.collection_id == normalized_collection)
    return normalized_collection, query, filters

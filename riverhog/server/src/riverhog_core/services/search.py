from __future__ import annotations

import math
from typing import Any

from riverhog_protocol.errors import BadRequest
from riverhog_protocol.paths import PathNormalizationError, normalize_collection_id
from sqlalchemy import asc, desc, func, literal, select
from sqlalchemy.sql.elements import ColumnElement

from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import CollectionFileRecord
from riverhog_core.runtime_config import RuntimeConfig

_SORT_FIELDS = {"logical_path", "collection_id", "collection_path", "bytes"}


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _order_expressions(
    sort: str,
    order: str,
) -> tuple[ColumnElement[Any], ...]:
    direction = desc if order == "desc" else asc
    if sort == "logical_path":
        return (
            direction(CollectionFileRecord.collection_id),
            direction(CollectionFileRecord.path),
        )
    if sort == "collection_id":
        return (
            direction(CollectionFileRecord.collection_id),
            asc(CollectionFileRecord.path),
        )
    if sort == "collection_path":
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
    def __init__(self, config: RuntimeConfig) -> None:
        self._session_factory = make_session_factory(config.database_url)

    def search(
        self,
        *,
        q: str | None,
        page: int,
        per_page: int,
        sort: str,
        order: str,
        collection: str | None = None,
        all_items: bool = False,
    ) -> dict[str, object]:
        if page < 1:
            raise BadRequest("page must be greater than or equal to 1")
        if per_page < 1 or per_page > 100:
            raise BadRequest("per_page must be between 1 and 100")
        if sort not in _SORT_FIELDS:
            raise BadRequest(f"sort must be one of {', '.join(sorted(_SORT_FIELDS))}")
        if order not in {"asc", "desc"}:
            raise BadRequest("order must be asc or desc")

        normalized_collection: str | None = None
        if collection:
            try:
                normalized_collection = normalize_collection_id(collection)
            except PathNormalizationError as exc:
                raise BadRequest(str(exc)) from exc

        logical_path_expr = (
            CollectionFileRecord.collection_id + literal("/") + CollectionFileRecord.path
        )
        filters: list[ColumnElement[bool]] = []
        query = q.strip() if q is not None else None
        if query:
            filters.append(
                func.lower(logical_path_expr).like(
                    _like_pattern(query.casefold()),
                    escape="\\",
                )
            )
        if normalized_collection is not None:
            filters.append(CollectionFileRecord.collection_id == normalized_collection)
        with session_scope(self._session_factory) as session:
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
            if not all_items:
                stmt = stmt.offset((page - 1) * per_page).limit(per_page)
            rows = session.execute(stmt).all()

        return {
            "query": query,
            "collection": normalized_collection,
            "page": 1 if all_items else page,
            "per_page": total_count if all_items else per_page,
            "total": total_count,
            "pages": (
                (1 if total_count else 0)
                if all_items
                else math.ceil(total_count / per_page)
                if total_count
                else 0
            ),
            "sort": sort,
            "order": order,
            "files": [
                {
                    "logical_path": f"{row.collection_id}/{row.path}",
                    "collection_id": row.collection_id,
                    "collection_path": row.path,
                    "bytes": row.bytes,
                    "sha256": row.sha256,
                }
                for row in rows
            ],
        }

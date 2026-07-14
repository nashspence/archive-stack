from __future__ import annotations

import math
from typing import Any

from sqlalchemy import asc, desc, exists, func, literal, select
from sqlalchemy.sql.elements import ColumnElement

from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import CollectionFileRecord, FileDiscRecord
from riverhog_core.domain.errors import BadRequest
from riverhog_core.fs_paths import PathNormalizationError, normalize_collection_id
from riverhog_core.runtime_config import RuntimeConfig

_SORT_FIELDS = {"target", "collection", "path", "bytes", "hot", "disc"}


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _order_expressions(
    sort: str,
    order: str,
    *,
    disc_coverage: ColumnElement[bool],
) -> tuple[ColumnElement[Any], ...]:
    direction = desc if order == "desc" else asc
    if sort == "target":
        return (
            direction(CollectionFileRecord.collection_id),
            direction(CollectionFileRecord.path),
        )
    if sort == "collection":
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
    if sort == "hot":
        return (
            direction(CollectionFileRecord.hot),
            asc(CollectionFileRecord.collection_id),
            asc(CollectionFileRecord.path),
        )
    if sort == "disc":
        return (
            direction(disc_coverage),
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
        hot: bool | None = None,
        disc_coverage: bool | None = None,
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

        target_expr = CollectionFileRecord.collection_id + literal("/") + CollectionFileRecord.path
        disc_coverage_expr = exists().where(
            FileDiscRecord.collection_id == CollectionFileRecord.collection_id,
            FileDiscRecord.path == CollectionFileRecord.path,
        )
        filters: list[ColumnElement[bool]] = []
        query = q.strip() if q is not None else None
        if query:
            filters.append(
                func.lower(target_expr).like(_like_pattern(query.casefold()), escape="\\")
            )
        if normalized_collection is not None:
            filters.append(CollectionFileRecord.collection_id == normalized_collection)
        if hot is not None:
            filters.append(CollectionFileRecord.hot.is_(hot))
        if disc_coverage is not None:
            filters.append(disc_coverage_expr == disc_coverage)

        with session_scope(self._session_factory) as session:
            total = session.scalar(
                select(func.count()).select_from(CollectionFileRecord).where(*filters)
            )
            total_count = int(total or 0)
            rows = session.execute(
                select(
                    CollectionFileRecord.collection_id,
                    CollectionFileRecord.path,
                    CollectionFileRecord.bytes,
                    CollectionFileRecord.sha256,
                    CollectionFileRecord.hot,
                    disc_coverage_expr.label("disc_coverage"),
                )
                .where(*filters)
                .order_by(*_order_expressions(sort, order, disc_coverage=disc_coverage_expr))
                .offset((page - 1) * per_page)
                .limit(per_page)
            ).all()

        return {
            "query": query,
            "collection": normalized_collection,
            "hot": hot,
            "disc_coverage": disc_coverage,
            "page": page,
            "per_page": per_page,
            "total": total_count,
            "pages": math.ceil(total_count / per_page) if total_count else 0,
            "sort": sort,
            "order": order,
            "files": [
                {
                    "target": f"{row.collection_id}/{row.path}",
                    "collection": row.collection_id,
                    "path": row.path,
                    "bytes": row.bytes,
                    "sha256": row.sha256,
                    "hot": row.hot,
                    "disc_coverage": row.disc_coverage,
                }
                for row in rows
            ],
        }

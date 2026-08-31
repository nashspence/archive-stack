from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from http_api_contracts import BrowseScalar, closed_literal_values
from riverhog_protocol import SearchSort, SortOrder
from riverhog_protocol.errors import BadRequest
from riverhog_protocol.paths import (
    PathNormalizationError,
    normalize_collection_id,
    text_search_key,
)
from sqlalchemy import asc, desc, select
from sqlalchemy.sql.elements import ColumnElement
from state_schema import read_snapshot

from riverhog_core.app_permissions import CATALOG_READ, ApplicationPrincipal
from riverhog_core.artifact_access import artifact_scope_filter
from riverhog_core.browse import bounded_page, keyset_statement, validate_page_size
from riverhog_core.catalog_db import SessionFactory, make_session_factory
from riverhog_core.catalog_models import CollectionFileRecord
from riverhog_core.collection_access import collection_access_filter, require_collection_access
from riverhog_core.runtime_config import RuntimeConfig

_SORT_FIELDS = closed_literal_values(SearchSort)
_SORT_ORDERS = closed_literal_values(SortOrder)


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


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
        page_size: int,
        position: tuple[str | int | bool | bytes | None, ...] | None,
        sort: str,
        order: str,
        collection: int | None = None,
        principal: ApplicationPrincipal | None = None,
    ) -> dict[str, object]:
        validate_page_size(page_size)
        if sort not in _SORT_FIELDS:
            raise BadRequest(f"sort must be one of {', '.join(sorted(_SORT_FIELDS))}")
        if order not in _SORT_ORDERS:
            raise BadRequest("order must be asc or desc")

        normalized_collection, query, _, stmt, key_columns = _search_statement(
            q=q,
            collection=collection,
            sort=sort,
            order=order,
            principal=principal,
        )
        with read_snapshot(self._session_factory) as session:
            if normalized_collection is not None:
                require_collection_access(
                    session,
                    principal,
                    CATALOG_READ,
                    normalized_collection,
                )
            rows, next_position = bounded_page(
                list(
                    session.execute(
                        keyset_statement(
                            stmt,
                            columns=key_columns,
                            position=position,
                            order=order,
                            page_size=page_size,
                        )
                    )
                ),
                page_size=page_size,
                position_of=lambda row: _search_position(row, sort=sort),
            )

        return {
            "query": query,
            "collection": normalized_collection,
            "page_size": page_size,
            "_next_position": next_position,
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
        if order not in _SORT_ORDERS:
            raise BadRequest("order must be asc or desc")
        normalized_collection, _query, _filters, statement, key_columns = _search_statement(
            q=q,
            collection=collection,
            sort=sort,
            order=order,
            principal=principal,
        )
        direction = desc if order == "desc" else asc
        statement = statement.order_by(*(direction(column) for column in key_columns))
        statement = statement.execution_options(yield_per=100)
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


def _search_statement(
    *,
    q: str | None,
    collection: int | None,
    sort: str,
    order: str,
    principal: ApplicationPrincipal | None,
) -> tuple[int | None, str | None, list[ColumnElement[bool]], Any, tuple[Any, ...]]:
    normalized_collection, query, filters = _search_filters(
        q=q,
        collection=collection,
        principal=principal,
    )
    statement = select(
        CollectionFileRecord.collection_id,
        CollectionFileRecord.path,
        CollectionFileRecord.path_sort_key,
        CollectionFileRecord.bytes,
        CollectionFileRecord.sha256,
    ).where(*filters)
    return normalized_collection, query, filters, statement, _key_columns(sort)


def _key_columns(sort: str) -> tuple[Any, ...]:
    if sort in {"file_ref", "collection_id"}:
        return CollectionFileRecord.collection_id, CollectionFileRecord.path_sort_key
    if sort == "path":
        return CollectionFileRecord.path_sort_key, CollectionFileRecord.collection_id
    if sort == "bytes":
        return (
            CollectionFileRecord.bytes,
            CollectionFileRecord.collection_id,
            CollectionFileRecord.path_sort_key,
        )
    raise BadRequest(f"sort must be one of {', '.join(sorted(_SORT_FIELDS))}")


def _search_position(row: Any, *, sort: str) -> tuple[BrowseScalar, ...]:
    if sort in {"file_ref", "collection_id"}:
        return row.collection_id, row.path_sort_key
    if sort == "path":
        return row.path_sort_key, row.collection_id
    return row.bytes, row.collection_id, row.path_sort_key


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
        filters.append(
            CollectionFileRecord.search_text.like(
                _like_pattern(text_search_key(query)),
                escape="\\",
            )
        )
    if normalized_collection is not None:
        filters.append(CollectionFileRecord.collection_id == normalized_collection)
    return normalized_collection, query, filters

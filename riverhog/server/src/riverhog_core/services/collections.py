from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from http_api_contracts import closed_literal_values
from riverhog_archive_contracts import normalize_passphrase_id
from riverhog_protocol import CollectionSort, SortOrder
from riverhog_protocol.errors import BadRequest, NotFound
from riverhog_protocol.paths import PathNormalizationError, normalize_collection_id
from sqlalchemy import asc, desc, func, select
from state_schema import read_snapshot

from riverhog_core.app_permissions import CATALOG_READ, ApplicationPrincipal
from riverhog_core.browse import bounded_page, keyset_statement
from riverhog_core.catalog_db import SessionFactory, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveCopyRecord,
    CollectionArchiveObjectRecord,
    CollectionRecord,
)
from riverhog_core.collection_access import collection_access_filter
from riverhog_core.domain.models import (
    CollectionListPage,
    CollectionSummary,
)
from riverhog_core.domain.types import CollectionId
from riverhog_core.runtime_config import RuntimeConfig

_COLLECTION_SORT_FIELDS = closed_literal_values(CollectionSort)
_SORT_ORDERS = closed_literal_values(SortOrder)


def _archive_object_field(object_id: str, column: Any) -> Any:
    return (
        select(column)
        .where(
            CollectionArchiveObjectRecord.collection_id
            == CollectionArchiveCopyRecord.collection_id,
            CollectionArchiveObjectRecord.store == CollectionArchiveCopyRecord.store,
            CollectionArchiveObjectRecord.object_id == object_id,
        )
        .correlate(CollectionArchiveCopyRecord)
        .scalar_subquery()
    )


def _collection_archive_copy_statement(collection_id: int) -> Any:
    object_count = (
        select(func.count())
        .select_from(CollectionArchiveObjectRecord)
        .where(
            CollectionArchiveObjectRecord.collection_id
            == CollectionArchiveCopyRecord.collection_id,
            CollectionArchiveObjectRecord.store == CollectionArchiveCopyRecord.store,
        )
        .correlate(CollectionArchiveCopyRecord)
        .scalar_subquery()
    )
    object_bytes = (
        select(func.coalesce(func.sum(CollectionArchiveObjectRecord.stored_bytes), 0))
        .where(
            CollectionArchiveObjectRecord.collection_id
            == CollectionArchiveCopyRecord.collection_id,
            CollectionArchiveObjectRecord.store == CollectionArchiveCopyRecord.store,
        )
        .correlate(CollectionArchiveCopyRecord)
        .scalar_subquery()
    )
    return (
        select(
            CollectionArchiveCopyRecord,
            object_count.label("object_count"),
            object_bytes.label("stored_bytes"),
            _archive_object_field("manifest", CollectionArchiveObjectRecord.object_path).label(
                "manifest_path"
            ),
            _archive_object_field("manifest", CollectionArchiveObjectRecord.sha256).label(
                "manifest_sha256"
            ),
        )
        .where(CollectionArchiveCopyRecord.collection_id == collection_id)
        .order_by(CollectionArchiveCopyRecord.store)
    )


def _collection_archive_copy_payload(row: Any) -> dict[str, object]:
    copy = row[0]
    publication_state = (
        "failed" if copy.state == "failed" else "uploaded" if row.manifest_path else "pending"
    )
    return {
        "store": copy.store,
        "state": copy.state,
        "storage_prefix": copy.archive_storage_prefix,
        "object_count": int(row.object_count),
        "stored_bytes": int(row.stored_bytes),
        "last_uploaded_at": copy.last_uploaded_at,
        "last_verified_at": copy.last_verified_at,
        "failure": copy.failure,
        "archive_root": {
            "object_path": row.manifest_path,
            "sha256": row.manifest_sha256,
            "state": publication_state,
        },
    }


class SqlAlchemyCollectionService:
    """Read the finalized collection catalog.

    Collection ingress is owned by ``SqlAlchemyCollectionUploadService``. Keeping that
    mutation boundary separate prevents the catalog reader from acquiring ingress
    responsibilities.
    """

    def __init__(
        self,
        config: RuntimeConfig,
        *,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._session_factory = session_factory or make_session_factory(config.database_url)

    def get(
        self,
        collection_id: int,
        *,
        principal: ApplicationPrincipal | None = None,
    ) -> CollectionSummary:
        normalized = _normalize_collection_id(collection_id)
        with session_scope(self._session_factory) as session:
            statement, _ = _collection_summary_query()
            row = session.execute(
                statement.where(
                    CollectionRecord.id == normalized,
                    CollectionRecord.is_published.is_(True),
                    collection_access_filter(CollectionRecord.id, principal, CATALOG_READ),
                )
            ).one_or_none()
            if row is None:
                raise NotFound(f"collection not found: {normalized}")
            return _collection_summary(row)

    def list(
        self,
        *,
        page_size: int,
        position: tuple[str | int | bool | bytes | None, ...] | None,
        q: str | None,
        encryption_format: str | None = None,
        passphrase_id: str | None = None,
        sort: str = "id",
        order: str = "asc",
        principal: ApplicationPrincipal | None = None,
    ) -> CollectionListPage:
        (
            _,
            normalized_format,
            normalized_passphrase_id,
            statement,
            key_columns,
        ) = _collection_list_statement(
            q=q,
            encryption_format=encryption_format,
            passphrase_id=passphrase_id,
            sort=sort,
            order=order,
            principal=principal,
        )
        with read_snapshot(self._session_factory) as session:
            rows, next_position = bounded_page(
                session.execute(
                    keyset_statement(
                        statement,
                        columns=key_columns,
                        position=position,
                        order=order,
                        page_size=page_size,
                    )
                ).all(),
                page_size=page_size,
                position_of=lambda row: _collection_position(row, sort=sort),
            )
            return CollectionListPage(
                page_size=page_size,
                next_position=next_position,
                sort=sort,
                order=order,
                query=q,
                encryption_format=normalized_format,
                passphrase_id=normalized_passphrase_id,
                collections=[_collection_summary(row) for row in rows],
            )

    def iter_collections(
        self,
        *,
        q: str | None,
        encryption_format: str | None = None,
        passphrase_id: str | None = None,
        sort: str = "id",
        order: str = "asc",
        principal: ApplicationPrincipal | None = None,
    ) -> Iterator[CollectionSummary]:
        _, _, _, statement, key_columns = _collection_list_statement(
            q=q,
            encryption_format=encryption_format,
            passphrase_id=passphrase_id,
            sort=sort,
            order=order,
            principal=principal,
        )
        ordering = desc if order == "desc" else asc
        statement = statement.order_by(*(ordering(column) for column in key_columns))
        with read_snapshot(self._session_factory) as session:
            rows = session.execute(statement.execution_options(yield_per=100))
            for partition in rows.partitions():
                for row in partition:
                    yield _collection_summary(row)

    def list_archive_copies(
        self,
        collection_id: int,
        *,
        page_size: int,
        position: tuple[str | int | bool | bytes | None, ...] | None,
        principal: ApplicationPrincipal | None = None,
    ) -> dict[str, object]:
        normalized = _normalize_collection_id(collection_id)
        with read_snapshot(self._session_factory) as session:
            collection = session.get(CollectionRecord, normalized)
            if collection is None or not collection.is_published:
                raise NotFound(f"collection not found: {normalized}")
            visible = collection_access_filter(CollectionRecord.id, principal, CATALOG_READ)
            if (
                session.scalar(
                    select(CollectionRecord.id).where(
                        CollectionRecord.id == normalized,
                        CollectionRecord.is_published.is_(True),
                        visible,
                    )
                )
                is None
            ):
                raise NotFound(f"collection not found: {normalized}")
            rows, next_position = bounded_page(
                session.execute(
                    keyset_statement(
                        _collection_archive_copy_statement(normalized).order_by(None),
                        columns=(CollectionArchiveCopyRecord.store,),
                        position=position,
                        order="asc",
                        page_size=page_size,
                    )
                ).all(),
                page_size=page_size,
                position_of=lambda row: (row[0].store,),
            )
            return {
                "collection_id": normalized,
                "page_size": page_size,
                "_next_position": next_position,
                "copies": [_collection_archive_copy_payload(row) for row in rows],
            }

    def iter_archive_copies(
        self,
        collection_id: int,
        *,
        principal: ApplicationPrincipal | None = None,
    ) -> Iterator[dict[str, object]]:
        normalized = _normalize_collection_id(collection_id)
        with read_snapshot(self._session_factory) as session:
            if (
                session.scalar(
                    select(CollectionRecord.id).where(
                        CollectionRecord.id == normalized,
                        CollectionRecord.is_published.is_(True),
                        collection_access_filter(CollectionRecord.id, principal, CATALOG_READ),
                    )
                )
                is None
            ):
                raise NotFound(f"collection not found: {normalized}")
            rows = session.execute(
                _collection_archive_copy_statement(normalized).execution_options(yield_per=100)
            )
            for row in rows:
                yield _collection_archive_copy_payload(row)


def _collection_list_statement(
    *,
    q: str | None,
    encryption_format: str | None,
    passphrase_id: str | None,
    sort: str,
    order: str,
    principal: ApplicationPrincipal | None,
) -> tuple[list[Any], str | None, str | None, Any, tuple[Any, ...]]:
    filters, normalized_format, normalized_passphrase_id = _collection_list_filters(
        q=q,
        encryption_format=encryption_format,
        passphrase_id=passphrase_id,
        sort=sort,
        order=order,
        principal=principal,
    )
    statement, sort_columns = _collection_summary_query()
    sort_column = sort_columns[sort]
    key_columns = (CollectionRecord.id,) if sort == "id" else (sort_column, CollectionRecord.id)
    return (
        filters,
        normalized_format,
        normalized_passphrase_id,
        statement.where(*filters),
        key_columns,
    )


def _collection_position(row: Any, *, sort: str) -> tuple[str | int, ...]:
    collection = row[0]
    value = {
        "id": collection.id,
        "created_at": collection.created_at,
        "bytes": collection.file_bytes,
        "files": collection.file_count,
    }[sort]
    return (value,) if sort == "id" else (value, collection.id)


def _collection_list_filters(
    *,
    q: str | None,
    encryption_format: str | None,
    passphrase_id: str | None,
    sort: str,
    order: str,
    principal: ApplicationPrincipal | None,
) -> tuple[list[Any], str | None, str | None]:
    if sort not in _COLLECTION_SORT_FIELDS:
        raise BadRequest(f"sort must be one of {', '.join(sorted(_COLLECTION_SORT_FIELDS))}")
    if order not in _SORT_ORDERS:
        raise BadRequest("order must be asc or desc")
    normalized_format = _normalize_filter(encryption_format, name="encryption_format")
    if passphrase_id is None:
        normalized_passphrase_id = None
    else:
        try:
            normalized_passphrase_id = normalize_passphrase_id(passphrase_id)
        except ValueError as exc:
            raise BadRequest(str(exc)) from exc
    filters = [
        CollectionRecord.is_published.is_(True),
        collection_access_filter(CollectionRecord.id, principal, CATALOG_READ),
    ]
    if q is not None:
        pattern = _like_pattern(q.casefold())
        matching_ids = select(CollectionRecord.id).where(
            CollectionRecord.search_text.like(pattern, escape="\\")
        )
        filters.append(CollectionRecord.id.in_(matching_ids))
    if normalized_format is not None:
        filters.append(CollectionRecord.encryption_format == normalized_format)
    if normalized_passphrase_id is not None:
        filters.append(CollectionRecord.passphrase_id == normalized_passphrase_id)
    return filters, normalized_format, normalized_passphrase_id


def _collection_summary_query() -> tuple[Any, dict[str, Any]]:
    archive_copy_count = (
        select(func.count())
        .select_from(CollectionArchiveCopyRecord)
        .where(CollectionArchiveCopyRecord.collection_id == CollectionRecord.id)
        .correlate(CollectionRecord)
        .scalar_subquery()
    )
    remote_storage_bytes = (
        select(func.coalesce(func.sum(CollectionArchiveObjectRecord.stored_bytes), 0))
        .where(CollectionArchiveObjectRecord.collection_id == CollectionRecord.id)
        .correlate(CollectionRecord)
        .scalar_subquery()
    )
    archive_root_sha256 = (
        select(func.min(CollectionArchiveObjectRecord.sha256))
        .where(
            CollectionArchiveObjectRecord.collection_id == CollectionRecord.id,
            CollectionArchiveObjectRecord.object_id == "manifest",
        )
        .correlate(CollectionRecord)
        .scalar_subquery()
    )
    archive_root_sha256_max = (
        select(func.max(CollectionArchiveObjectRecord.sha256))
        .where(
            CollectionArchiveObjectRecord.collection_id == CollectionRecord.id,
            CollectionArchiveObjectRecord.object_id == "manifest",
        )
        .correlate(CollectionRecord)
        .scalar_subquery()
    )
    return (
        select(
            CollectionRecord,
            CollectionRecord.file_count.label("files"),
            CollectionRecord.file_bytes.label("bytes"),
            archive_copy_count.label("archive_copy_count"),
            remote_storage_bytes.label("remote_storage_bytes"),
            archive_root_sha256.label("archive_root_sha256"),
            archive_root_sha256_max.label("archive_root_sha256_max"),
        ),
        {
            "id": CollectionRecord.id,
            "created_at": CollectionRecord.created_at,
            "bytes": CollectionRecord.file_bytes,
            "files": CollectionRecord.file_count,
        },
    )


def _collection_summary(
    row: Any,
) -> CollectionSummary:
    collection = row[0]
    if row.archive_root_sha256 is None or row.archive_root_sha256 != row.archive_root_sha256_max:
        raise RuntimeError("finalized collection has no unambiguous archive-root identity")
    return CollectionSummary(
        id=CollectionId(collection.id),
        created_at=collection.created_at,
        content_identity=collection.content_identity,
        archive_root_sha256=str(row.archive_root_sha256),
        encryption_format=collection.encryption_format,
        passphrase_id=collection.passphrase_id,
        files=int(row.files),
        bytes=int(row.bytes),
        remote_storage_bytes=int(row.remote_storage_bytes),
        archive_copy_count=int(row.archive_copy_count),
    )


def _normalize_filter(value: str | None, *, name: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized or normalized != value or len(normalized) > 128:
        raise BadRequest(f"{name} is invalid")
    return normalized


def _normalize_collection_id(value: str | int) -> int:
    try:
        return normalize_collection_id(value)
    except PathNormalizationError as exc:
        raise BadRequest(str(exc)) from exc


_normalize_collection_id_or_raise = _normalize_collection_id


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"

from __future__ import annotations

from typing import Any

from riverhog_archive_contracts import normalize_passphrase_id
from riverhog_protocol.errors import BadRequest, NotFound
from riverhog_protocol.paths import PathNormalizationError, normalize_collection_id, normalize_tag
from sqlalchemy import String, cast, exists, func, or_, select
from sqlalchemy.orm import selectinload

from riverhog_core.app_permissions import CATALOG_READ, ApplicationPrincipal
from riverhog_core.catalog_db import SessionFactory, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveCopyRecord,
    CollectionArchiveObjectRecord,
    CollectionFileRecord,
    CollectionRecord,
    CollectionTagRecord,
)
from riverhog_core.collection_access import collection_access_filter
from riverhog_core.domain.enums import ArchiveState
from riverhog_core.domain.models import (
    ArchiveCopyStatus,
    ArchiveRootPublicationStatus,
    CollectionListPage,
    CollectionSummary,
)
from riverhog_core.domain.types import CollectionId
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_records import (
    ArchiveCopyAggregate,
    archive_copy_aggregates,
)

_COLLECTION_SORT_FIELDS = {"id", "created_at", "bytes", "files"}


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
                    collection_access_filter(CollectionRecord.id, principal, CATALOG_READ),
                )
            ).one_or_none()
            if row is None:
                raise NotFound(f"collection not found: {normalized}")
            return _collection_summary(
                row,
                aggregates=archive_copy_aggregates(session, collection_ids=[normalized]),
            )

    def list(
        self,
        *,
        page: int,
        per_page: int,
        q: str | None,
        tag: str | None = None,
        encryption_format: str | None = None,
        passphrase_id: str | None = None,
        sort: str = "id",
        order: str = "asc",
        all_items: bool = False,
        principal: ApplicationPrincipal | None = None,
    ) -> CollectionListPage:
        if page < 1:
            raise BadRequest("page must be at least 1")
        if per_page < 1:
            raise BadRequest("per_page must be at least 1")
        if sort not in _COLLECTION_SORT_FIELDS:
            raise BadRequest(f"sort must be one of {', '.join(sorted(_COLLECTION_SORT_FIELDS))}")
        if order not in {"asc", "desc"}:
            raise BadRequest("order must be asc or desc")
        normalized_tag = _normalize_tag(tag) if tag is not None else None
        normalized_format = _normalize_filter(encryption_format, name="encryption_format")
        if passphrase_id is None:
            normalized_passphrase_id = None
        else:
            try:
                normalized_passphrase_id = normalize_passphrase_id(passphrase_id)
            except ValueError as exc:
                raise BadRequest(str(exc)) from exc
        filters = [collection_access_filter(CollectionRecord.id, principal, CATALOG_READ)]
        if q is not None:
            filters.append(
                or_(
                    cast(CollectionRecord.id, String).like(
                        _like_pattern(q.casefold()), escape="\\"
                    ),
                    exists(
                        select(1).where(
                            CollectionTagRecord.collection_id == CollectionRecord.id,
                            func.lower(CollectionTagRecord.tag_id).like(
                                _like_pattern(q.casefold()),
                                escape="\\",
                            ),
                        )
                    ),
                )
            )
        if normalized_tag is not None:
            filters.append(
                exists(
                    select(1).where(
                        CollectionTagRecord.collection_id == CollectionRecord.id,
                        CollectionTagRecord.tag_id == normalized_tag,
                    )
                )
            )
        if normalized_format is not None:
            filters.append(CollectionRecord.encryption_format == normalized_format)
        if normalized_passphrase_id is not None:
            filters.append(CollectionRecord.passphrase_id == normalized_passphrase_id)
        with session_scope(self._session_factory) as session:
            total = int(
                session.scalar(select(func.count()).select_from(CollectionRecord).where(*filters))
                or 0
            )
            statement, sort_columns = _collection_summary_query()
            sort_column = sort_columns[sort]
            direction = sort_column.desc() if order == "desc" else sort_column.asc()
            statement = statement.where(*filters).order_by(
                direction,
                CollectionRecord.id.asc(),
            )
            if not all_items:
                statement = statement.offset((page - 1) * per_page).limit(per_page)
            rows = session.execute(statement).all()
            aggregates = archive_copy_aggregates(
                session,
                collection_ids=[row[0].id for row in rows],
            )
            return CollectionListPage(
                page=1 if all_items else page,
                per_page=total if all_items else per_page,
                total=total,
                pages=(1 if total else 0)
                if all_items
                else ((total + per_page - 1) // per_page if total else 0),
                sort=sort,
                order=order,
                query=q,
                tag=normalized_tag,
                encryption_format=normalized_format,
                passphrase_id=normalized_passphrase_id,
                collections=[_collection_summary(row, aggregates=aggregates) for row in rows],
            )


def _collection_summary_query() -> tuple[Any, dict[str, Any]]:
    file_stats = (
        select(
            CollectionFileRecord.collection_id.label("collection_id"),
            func.count(CollectionFileRecord.path).label("files"),
            func.coalesce(func.sum(CollectionFileRecord.bytes), 0).label("bytes"),
        )
        .group_by(CollectionFileRecord.collection_id)
        .subquery()
    )
    files = func.coalesce(file_stats.c.files, 0)
    byte_count = func.coalesce(file_stats.c.bytes, 0)
    return (
        select(
            CollectionRecord,
            files.label("files"),
            byte_count.label("bytes"),
        )
        .options(
            selectinload(CollectionRecord.archive_copies).selectinload(
                CollectionArchiveCopyRecord.objects.and_(
                    CollectionArchiveObjectRecord.object_id.in_(("manifest", "proof"))
                )
            ),
            selectinload(CollectionRecord.tags),
        )
        .outerjoin(file_stats, file_stats.c.collection_id == CollectionRecord.id),
        {
            "id": CollectionRecord.id,
            "created_at": CollectionRecord.created_at,
            "bytes": byte_count,
            "files": files,
        },
    )


def _collection_summary(
    row: Any,
    *,
    aggregates: dict[tuple[int, str], ArchiveCopyAggregate],
) -> CollectionSummary:
    collection = row[0]
    copies = tuple(
        _archive_copy_status(current, aggregates=aggregates)
        for current in sorted(collection.archive_copies, key=lambda value: value.store)
    )
    return CollectionSummary(
        id=CollectionId(collection.id),
        created_at=collection.created_at,
        tags=tuple(sorted(current.tag_id for current in collection.tags)),
        content_identity=collection.content_identity,
        archive_root_sha256=_archive_root_identity(copies),
        encryption_format=collection.encryption_format,
        passphrase_id=collection.passphrase_id,
        files=int(row.files),
        bytes=int(row.bytes),
        remote_storage_bytes=sum(current.stored_bytes or 0 for current in copies),
        archive_copies=copies,
    )


def _normalize_filter(value: str | None, *, name: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized or normalized != value or len(normalized) > 128:
        raise BadRequest(f"{name} is invalid")
    return normalized


def _archive_copy_status(
    archive: CollectionArchiveCopyRecord,
    *,
    aggregates: dict[tuple[int, str], ArchiveCopyAggregate],
) -> ArchiveCopyStatus:
    object_count, stored_bytes = aggregates.get((archive.collection_id, archive.store), (0, 0))
    return ArchiveCopyStatus(
        store=archive.store,
        state=ArchiveState(archive.state),
        storage_prefix=archive.archive_storage_prefix,
        object_count=object_count,
        stored_bytes=stored_bytes,
        last_uploaded_at=archive.last_uploaded_at,
        last_verified_at=archive.last_verified_at,
        failure=archive.failure,
        archive_root=_archive_root_status(archive),
    )


def _archive_root_status(archive: CollectionArchiveCopyRecord) -> ArchiveRootPublicationStatus:
    manifest = next(
        (current for current in archive.objects if current.object_id == "manifest"),
        None,
    )
    proof = next(
        (current for current in archive.objects if current.object_id == "proof"),
        None,
    )
    return ArchiveRootPublicationStatus(
        object_path=manifest.object_path if manifest else None,
        sha256=manifest.sha256 if manifest else None,
        proof_object_path=proof.object_path if proof else None,
        proof_state=(
            "failed"
            if archive.state == ArchiveState.FAILED.value
            else "uploaded"
            if proof
            else "pending"
        ),
        proof_sha256=proof.sha256 if proof else None,
    )


def _normalize_collection_id(value: str | int) -> int:
    try:
        return normalize_collection_id(value)
    except PathNormalizationError as exc:
        raise BadRequest(str(exc)) from exc


_normalize_collection_id_or_raise = _normalize_collection_id


def _normalize_tag(value: str) -> str:
    try:
        normalized = normalize_tag(value)
    except PathNormalizationError as exc:
        raise BadRequest(str(exc)) from exc
    if normalized != value:
        raise BadRequest("tag id must be canonical")
    return normalized


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _archive_root_identity(copies: tuple[ArchiveCopyStatus, ...]) -> str:
    identities = {
        current.archive_root.sha256
        for current in copies
        if current.archive_root is not None and current.archive_root.sha256
    }
    if len(identities) != 1:
        raise RuntimeError("finalized collection has no unambiguous archive-root identity")
    return str(next(iter(identities)))

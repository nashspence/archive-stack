from __future__ import annotations

import json
import math
from collections.abc import Sequence
from typing import Any

from sqlalchemy import case, delete, func, insert, literal, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveCopyRecord,
    CollectionArchiveFileObjectRecord,
    CollectionArchiveObjectRecord,
    CollectionFileRecord,
    CollectionRecord,
    FetchFileRecord,
    FetchRecord,
)
from riverhog_core.domain.enums import FetchState
from riverhog_core.domain.errors import (
    BadRequest,
    Conflict,
    InvalidState,
    NotFound,
    ServiceUnavailable,
)
from riverhog_core.domain.models import FetchListPage, FetchSummary
from riverhog_core.domain.types import CollectionId, FetchId
from riverhog_core.fs_paths import (
    PathNormalizationError,
    normalize_collection_id,
    normalize_relpath,
)
from riverhog_core.ports.archive_store import (
    ArchiveObjectIdentity,
    ArchiveVerificationError,
    CollectionArchiveIdentity,
)
from riverhog_core.ports.hot_store import HotStore
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_records import archive_copy_is_complete
from riverhog_core.services.collection_custody import require_collection_custody_idle

_FETCH_SORT_FIELDS = {"id", "name", "state", "order", "files", "bytes", "missing_bytes"}
_FETCH_FILE_SORT_FIELDS = {
    "logical_path",
    "collection_id",
    "collection_path",
    "bytes",
    "hot",
}

FileSelection = tuple[str, str]


class SqlAlchemyFetchService:
    def __init__(
        self,
        config: RuntimeConfig,
        archive_stores: ArchiveStoreRegistry,
        hot_store: HotStore,
    ) -> None:
        self._archive_stores = archive_stores
        self._hot_store = hot_store
        self._session_factory = make_session_factory(config.database_url)

    def create(
        self,
        *,
        name: str,
        collections: Sequence[str] | None = None,
        files: Sequence[FileSelection] | None = None,
    ) -> FetchSummary:
        collection_ids = _canonical_collection_ids(collections or ())
        file_ids = _canonical_files(files or ())
        with session_scope(self._session_factory) as session:
            _require_collections_exist(session, collection_ids)
            _require_files_exist(session, file_ids)
            fetch_order = _next_fetch_order(session)
            fetch = FetchRecord(
                fetch_id=f"fx-{fetch_order}",
                name=_normalize_fetch_name(name),
                fetch_order=fetch_order,
                fetch_state=FetchState.DRAFT.value,
            )
            session.add(fetch)
            session.flush()
            _add_fetch_collections(session, fetch.fetch_id, collection_ids)
            _add_fetch_files(session, fetch.fetch_id, file_ids)
            return _fetch_summary(session, fetch.fetch_id)

    def list(
        self,
        *,
        page: int,
        per_page: int,
        state: str | None = None,
        q: str | None = None,
        sort: str = "order",
        order: str = "asc",
        all_items: bool = False,
    ) -> FetchListPage:
        if page < 1:
            raise BadRequest("page must be at least 1")
        if per_page < 1 or per_page > 100:
            raise BadRequest("per_page must be between 1 and 100")
        if state is not None and state not in {item.value for item in FetchState}:
            raise BadRequest("state must be a valid fetch state")
        if sort not in _FETCH_SORT_FIELDS:
            raise BadRequest(f"sort must be one of {', '.join(sorted(_FETCH_SORT_FIELDS))}")
        if order not in {"asc", "desc"}:
            raise BadRequest("order must be asc or desc")

        stmt, expressions = _fetch_summary_query()
        if state is not None:
            stmt = stmt.where(FetchRecord.fetch_state == state)
        if q:
            pattern = _like_pattern(q.casefold())
            logical_path = FetchFileRecord.collection_id + literal("/") + FetchFileRecord.path
            file_match = (
                select(FetchFileRecord.fetch_id)
                .where(
                    FetchFileRecord.fetch_id == FetchRecord.fetch_id,
                    func.lower(logical_path).like(pattern, escape="\\"),
                )
                .exists()
            )
            stmt = stmt.where(
                or_(
                    func.lower(FetchRecord.fetch_id).like(pattern, escape="\\"),
                    func.lower(FetchRecord.name).like(pattern, escape="\\"),
                    file_match,
                )
            )
        sort_expr = {
            "id": FetchRecord.fetch_id,
            "name": func.lower(FetchRecord.name),
            "state": FetchRecord.fetch_state,
            "order": FetchRecord.fetch_order,
            "files": expressions["files"],
            "bytes": expressions["bytes"],
            "missing_bytes": expressions["missing_bytes"],
        }[sort]
        stmt = stmt.order_by(
            sort_expr.desc() if order == "desc" else sort_expr.asc(),
            FetchRecord.fetch_id,
        )
        with session_scope(self._session_factory) as session:
            total = int(session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
            selected = stmt if all_items else stmt.offset((page - 1) * per_page).limit(per_page)
            rows = session.execute(selected).all()
            fetch_ids = [row[0].fetch_id for row in rows]
            collections_by_fetch = _collections_by_fetch(session, fetch_ids)
            summaries = [
                _fetch_summary_from_row(row, collections_by_fetch.get(row[0].fetch_id, ()))
                for row in rows
            ]
        return FetchListPage(
            page=1 if all_items else page,
            per_page=total if all_items else per_page,
            total=total,
            pages=(1 if total else 0) if all_items else math.ceil(total / per_page) if total else 0,
            fetches=summaries,
        )

    def add_collections(self, fetch_id: str, collections: Sequence[str]) -> FetchSummary:
        collection_ids = _canonical_collection_ids(collections)
        if not collection_ids:
            raise BadRequest("at least one collection is required")
        with session_scope(self._session_factory) as session:
            fetch = _get_fetch(session, fetch_id)
            _require_editable(fetch)
            _require_collections_exist(session, collection_ids)
            _add_fetch_collections(session, fetch_id, collection_ids)
            return _fetch_summary(session, fetch_id)

    def remove_collections(self, fetch_id: str, collections: Sequence[str]) -> FetchSummary:
        collection_ids = _canonical_collection_ids(collections)
        if not collection_ids:
            raise BadRequest("at least one collection is required")
        with session_scope(self._session_factory) as session:
            fetch = _get_fetch(session, fetch_id)
            _require_editable(fetch)
            session.execute(
                delete(FetchFileRecord).where(
                    FetchFileRecord.fetch_id == fetch_id,
                    FetchFileRecord.collection_id.in_(collection_ids),
                )
            )
            return _fetch_summary(session, fetch_id)

    def add_files(self, fetch_id: str, files: Sequence[FileSelection]) -> FetchSummary:
        file_ids = _canonical_files(files)
        if not file_ids:
            raise BadRequest("at least one file is required")
        with session_scope(self._session_factory) as session:
            fetch = _get_fetch(session, fetch_id)
            _require_editable(fetch)
            _require_files_exist(session, file_ids)
            _add_fetch_files(session, fetch_id, file_ids)
            return _fetch_summary(session, fetch_id)

    def remove_files(self, fetch_id: str, files: Sequence[FileSelection]) -> FetchSummary:
        file_ids = _canonical_files(files)
        if not file_ids:
            raise BadRequest("at least one file is required")
        with session_scope(self._session_factory) as session:
            fetch = _get_fetch(session, fetch_id)
            _require_editable(fetch)
            for collection_id, path in file_ids:
                session.execute(
                    delete(FetchFileRecord).where(
                        FetchFileRecord.fetch_id == fetch_id,
                        FetchFileRecord.collection_id == collection_id,
                        FetchFileRecord.path == path,
                    )
                )
            return _fetch_summary(session, fetch_id)

    def start(self, fetch_id: str) -> FetchSummary:
        with session_scope(self._session_factory) as session:
            fetch = _get_fetch(session, fetch_id)
            _require_startable(fetch)
            collection_ids = _fetch_collection_ids(session, fetch_id)
            if not collection_ids:
                raise InvalidState("fetch has no files")
            for collection_id in collection_ids:
                require_collection_custody_idle(session, collection_id)
            summary = _fetch_summary(session, fetch_id)
            fetch.fetch_state = (
                FetchState.DONE.value
                if summary.hot_files == summary.files
                else FetchState.QUEUED_ARCHIVE.value
            )
            return _fetch_summary(session, fetch_id)

    def cancel(self, fetch_id: str) -> FetchSummary:
        with session_scope(self._session_factory) as session:
            fetch = _get_fetch(session, fetch_id)
            if fetch.fetch_state == FetchState.DONE.value:
                raise InvalidState("completed fetches cannot be canceled")
            fetch.fetch_state = FetchState.DRAFT.value
            return _fetch_summary(session, fetch_id)

    def evict(
        self,
        collections: Sequence[str] = (),
        *,
        files: Sequence[FileSelection] = (),
        dry_run: bool = False,
    ) -> dict[str, object]:
        collection_ids = _canonical_collection_ids(collections)
        file_ids = _canonical_files(files)
        if not collection_ids and not file_ids:
            raise BadRequest("at least one collection or file is required")
        with session_scope(self._session_factory) as session:
            _require_collections_exist(session, collection_ids)
            _require_files_exist(session, file_ids)
            selected_query = _selected_files_query(collection_ids, file_ids)
            selected = session.scalars(
                selected_query.order_by(
                    CollectionFileRecord.collection_id,
                    CollectionFileRecord.path,
                )
            ).all()
            if not selected:
                raise NotFound("selection contains no files")
            selected_collections = sorted({current.collection_id for current in selected})
            for collection_id in selected_collections:
                require_collection_custody_idle(session, collection_id)
            selected_files, selected_bytes = _file_query_stats(session, selected_query)
            hot_query = selected_query.where(CollectionFileRecord.hot.is_(True))
            would_evict_files, would_evict_bytes = _file_query_stats(session, hot_query)
            identities = {
                collection_id: _selected_archive_identities(
                    session,
                    collection_id=collection_id,
                    paths=[
                        current.path
                        for current in selected
                        if current.collection_id == collection_id
                    ],
                )
                for collection_id in selected_collections
            }
            if any(not copies for copies in identities.values()):
                missing = next(
                    collection_id for collection_id, copies in identities.items() if not copies
                )
                raise Conflict(
                    "cannot evict files before their archive objects are uploaded: " + missing
                )
            if dry_run:
                return _eviction_payload(
                    collections=selected_collections,
                    files=[(current.collection_id, current.path) for current in selected],
                    selected_files=selected_files,
                    selected_bytes=selected_bytes,
                    would_evict_files=would_evict_files,
                    would_evict_bytes=would_evict_bytes,
                    dry_run=True,
                )
            for collection_id, copies in identities.items():
                failures: list[Exception] = []
                for store_name, identity in copies:
                    try:
                        self._archive_stores.require(store_name).verify_collection_archive(
                            collection_id=collection_id,
                            archive=identity,
                        )
                        break
                    except Exception as exc:
                        failures.append(exc)
                else:
                    if failures and all(
                        isinstance(exc, ArchiveVerificationError) for exc in failures
                    ):
                        raise Conflict(
                            "no archive copy matches its upload record: " + collection_id
                        ) from failures[-1]
                    raise ServiceUnavailable(
                        "cannot confirm a remote archive copy before hot eviction: " + collection_id
                    ) from failures[-1]
            for record in selected:
                if not record.hot:
                    continue
                try:
                    self._hot_store.delete_collection_file(record.collection_id, record.path)
                except FileNotFoundError:
                    pass
                record.hot = False
            return _eviction_payload(
                collections=selected_collections,
                files=[(current.collection_id, current.path) for current in selected],
                selected_files=selected_files,
                selected_bytes=selected_bytes,
                would_evict_files=would_evict_files,
                would_evict_bytes=would_evict_bytes,
                dry_run=False,
            )

    def get(self, fetch_id: str) -> FetchSummary:
        with session_scope(self._session_factory) as session:
            return _fetch_summary(session, fetch_id)

    def status(self, fetch_id: str) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            summary = _fetch_summary(session, fetch_id)
            collection_rows = session.execute(_fetch_collection_summary_query(fetch_id)).all()
            files = session.scalars(
                _fetch_files_query(fetch_id)
                .order_by(CollectionFileRecord.collection_id, CollectionFileRecord.path)
                .limit(25)
            ).all()
            return {
                **_fetch_summary_payload(summary),
                "collection_summaries": [
                    {
                        "collection_id": row.collection_id,
                        "files": int(row.files),
                        "bytes": int(row.bytes),
                        "hot_files": int(row.hot_files),
                        "hot_bytes": int(row.hot_bytes),
                        "missing_files": int(row.missing_files),
                        "missing_bytes": int(row.missing_bytes),
                    }
                    for row in collection_rows
                ],
                "files_preview": [_file_payload(file) for file in files],
                "next_action": _next_action(summary),
            }

    def files(
        self,
        fetch_id: str,
        *,
        page: int,
        per_page: int,
        sort: str,
        order: str,
        q: str | None = None,
        hot: bool | None = None,
    ) -> dict[str, object]:
        if page < 1:
            raise BadRequest("page must be at least 1")
        if per_page < 1 or per_page > 100:
            raise BadRequest("per_page must be between 1 and 100")
        if sort not in _FETCH_FILE_SORT_FIELDS:
            raise BadRequest(f"sort must be one of {', '.join(sorted(_FETCH_FILE_SORT_FIELDS))}")
        if order not in {"asc", "desc"}:
            raise BadRequest("order must be asc or desc")
        stmt = _fetch_files_query(fetch_id)
        if q:
            logical_path = (
                CollectionFileRecord.collection_id + literal("/") + CollectionFileRecord.path
            )
            stmt = stmt.where(
                func.lower(logical_path).like(_like_pattern(q.casefold()), escape="\\")
            )
        if hot is not None:
            stmt = stmt.where(CollectionFileRecord.hot.is_(hot))
        with session_scope(self._session_factory) as session:
            _get_fetch(session, fetch_id)
            total = int(session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
            records = session.scalars(
                stmt.order_by(*_fetch_file_order_by(sort=sort, order=order))
                .offset((page - 1) * per_page)
                .limit(per_page)
            ).all()
        return {
            "fetch_id": fetch_id,
            "q": q,
            "hot": hot,
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": math.ceil(total / per_page) if total else 0,
            "sort": sort,
            "order": order,
            "files": [_file_payload(file) for file in records],
        }


def _selected_archive_identities(
    session: Session,
    *,
    collection_id: str,
    paths: Sequence[str],
) -> list[tuple[str, CollectionArchiveIdentity]]:
    copies = session.scalars(
        select(CollectionArchiveCopyRecord)
        .where(CollectionArchiveCopyRecord.collection_id == collection_id)
        .order_by(CollectionArchiveCopyRecord.store)
    ).all()
    result: list[tuple[str, CollectionArchiveIdentity]] = []
    for copy in copies:
        if not archive_copy_is_complete(copy):
            continue
        required_ids = select(CollectionArchiveFileObjectRecord.object_id).where(
            CollectionArchiveFileObjectRecord.collection_id == collection_id,
            CollectionArchiveFileObjectRecord.store == copy.store,
            CollectionArchiveFileObjectRecord.path.in_(paths),
        )
        objects = session.scalars(
            select(CollectionArchiveObjectRecord)
            .where(
                CollectionArchiveObjectRecord.collection_id == collection_id,
                CollectionArchiveObjectRecord.store == copy.store,
                or_(
                    CollectionArchiveObjectRecord.object_id.in_(("manifest", "proof")),
                    CollectionArchiveObjectRecord.object_id.in_(required_ids),
                ),
            )
            .order_by(CollectionArchiveObjectRecord.object_order)
        ).all()
        if not objects:
            continue
        result.append(
            (
                copy.store,
                CollectionArchiveIdentity(
                    objects=tuple(
                        ArchiveObjectIdentity(
                            object_id=current.object_id,
                            kind=current.kind,
                            object_path=current.object_path,
                            plaintext_bytes=current.plaintext_bytes,
                            stored_bytes=current.stored_bytes,
                            sha256=current.sha256,
                        )
                        for current in objects
                    )
                ),
            )
        )
    return result


def _fetch_summary_query() -> tuple[Any, dict[str, Any]]:
    stats = (
        select(
            FetchFileRecord.fetch_id.label("fetch_id"),
            func.count(CollectionFileRecord.path).label("files"),
            func.coalesce(func.sum(CollectionFileRecord.bytes), 0).label("bytes"),
            func.coalesce(
                func.sum(case((CollectionFileRecord.hot.is_(True), 1), else_=0)),
                0,
            ).label("hot_files"),
            func.coalesce(
                func.sum(
                    case(
                        (CollectionFileRecord.hot.is_(True), CollectionFileRecord.bytes),
                        else_=0,
                    )
                ),
                0,
            ).label("hot_bytes"),
        )
        .join(
            CollectionFileRecord,
            (CollectionFileRecord.collection_id == FetchFileRecord.collection_id)
            & (CollectionFileRecord.path == FetchFileRecord.path),
        )
        .group_by(FetchFileRecord.fetch_id)
        .subquery()
    )
    files = func.coalesce(stats.c.files, 0)
    bytes_total = func.coalesce(stats.c.bytes, 0)
    hot_files = func.coalesce(stats.c.hot_files, 0)
    hot_bytes = func.coalesce(stats.c.hot_bytes, 0)
    expressions = {
        "files": files,
        "bytes": bytes_total,
        "hot_files": hot_files,
        "hot_bytes": hot_bytes,
        "missing_files": files - hot_files,
        "missing_bytes": bytes_total - hot_bytes,
    }
    return (
        select(
            FetchRecord,
            expressions["files"].label("files"),
            expressions["bytes"].label("bytes"),
            expressions["hot_files"].label("hot_files"),
            expressions["hot_bytes"].label("hot_bytes"),
            expressions["missing_files"].label("missing_files"),
            expressions["missing_bytes"].label("missing_bytes"),
        ).outerjoin(stats, stats.c.fetch_id == FetchRecord.fetch_id),
        expressions,
    )


def _fetch_collection_summary_query(fetch_id: str) -> Any:
    files = func.count(CollectionFileRecord.path)
    bytes_total = func.coalesce(func.sum(CollectionFileRecord.bytes), 0)
    hot_files = func.coalesce(
        func.sum(case((CollectionFileRecord.hot.is_(True), 1), else_=0)),
        0,
    )
    hot_bytes = func.coalesce(
        func.sum(
            case(
                (CollectionFileRecord.hot.is_(True), CollectionFileRecord.bytes),
                else_=0,
            )
        ),
        0,
    )
    return (
        select(
            FetchFileRecord.collection_id.label("collection_id"),
            files.label("files"),
            bytes_total.label("bytes"),
            hot_files.label("hot_files"),
            hot_bytes.label("hot_bytes"),
            (files - hot_files).label("missing_files"),
            (bytes_total - hot_bytes).label("missing_bytes"),
        )
        .select_from(FetchFileRecord)
        .join(
            CollectionFileRecord,
            (CollectionFileRecord.collection_id == FetchFileRecord.collection_id)
            & (CollectionFileRecord.path == FetchFileRecord.path),
        )
        .where(FetchFileRecord.fetch_id == fetch_id)
        .group_by(FetchFileRecord.collection_id)
        .order_by(func.min(FetchFileRecord.file_order), FetchFileRecord.collection_id)
    )


def _fetch_files_query(fetch_id: str) -> Any:
    return (
        select(CollectionFileRecord)
        .join(
            FetchFileRecord,
            (FetchFileRecord.collection_id == CollectionFileRecord.collection_id)
            & (FetchFileRecord.path == CollectionFileRecord.path),
        )
        .where(FetchFileRecord.fetch_id == fetch_id)
    )


def _selected_files_query(
    collection_ids: Sequence[str],
    file_ids: Sequence[FileSelection],
) -> Any:
    predicates: list[ColumnElement[bool]] = []
    if collection_ids:
        predicates.append(CollectionFileRecord.collection_id.in_(collection_ids))
    predicates.extend(
        (CollectionFileRecord.collection_id == collection_id) & (CollectionFileRecord.path == path)
        for collection_id, path in file_ids
    )
    return select(CollectionFileRecord).where(or_(*predicates)).distinct()


def _file_query_stats(session: Session, query: Any) -> tuple[int, int]:
    selected = query.with_only_columns(
        CollectionFileRecord.collection_id,
        CollectionFileRecord.path,
        CollectionFileRecord.bytes,
    ).subquery()
    files, bytes_total = session.execute(
        select(func.count(), func.coalesce(func.sum(selected.c.bytes), 0)).select_from(selected)
    ).one()
    return int(files), int(bytes_total)


def _add_fetch_collections(
    session: Session,
    fetch_id: str,
    collection_ids: Sequence[str],
) -> None:
    if not collection_ids:
        return
    next_order = int(
        session.scalar(
            select(func.coalesce(func.max(FetchFileRecord.file_order), 0)).where(
                FetchFileRecord.fetch_id == fetch_id
            )
        )
        or 0
    )
    exists = (
        select(FetchFileRecord.fetch_id)
        .where(
            FetchFileRecord.fetch_id == fetch_id,
            FetchFileRecord.collection_id == CollectionFileRecord.collection_id,
            FetchFileRecord.path == CollectionFileRecord.path,
        )
        .exists()
    )
    rows = (
        select(
            literal(fetch_id),
            CollectionFileRecord.collection_id,
            CollectionFileRecord.path,
            (
                next_order
                + func.row_number().over(
                    order_by=(CollectionFileRecord.collection_id, CollectionFileRecord.path)
                )
            ),
        )
        .where(CollectionFileRecord.collection_id.in_(collection_ids), ~exists)
        .order_by(CollectionFileRecord.collection_id, CollectionFileRecord.path)
    )
    session.execute(
        insert(FetchFileRecord).from_select(
            ["fetch_id", "collection_id", "path", "file_order"],
            rows,
        )
    )


def _add_fetch_files(
    session: Session,
    fetch_id: str,
    files: Sequence[FileSelection],
) -> None:
    next_order = int(
        session.scalar(
            select(func.coalesce(func.max(FetchFileRecord.file_order), 0)).where(
                FetchFileRecord.fetch_id == fetch_id
            )
        )
        or 0
    )
    existing = set(
        session.execute(
            select(FetchFileRecord.collection_id, FetchFileRecord.path).where(
                FetchFileRecord.fetch_id == fetch_id
            )
        ).all()
    )
    for collection_id, path in files:
        if (collection_id, path) in existing:
            continue
        next_order += 1
        session.add(
            FetchFileRecord(
                fetch_id=fetch_id,
                collection_id=collection_id,
                path=path,
                file_order=next_order,
            )
        )
    session.flush()


def _collections_by_fetch(
    session: Session,
    fetch_ids: Sequence[str],
) -> dict[str, list[str]]:
    if not fetch_ids:
        return {}
    ordered = (
        select(
            FetchFileRecord.fetch_id,
            FetchFileRecord.collection_id,
            func.min(FetchFileRecord.file_order).label("first_order"),
        )
        .where(FetchFileRecord.fetch_id.in_(fetch_ids))
        .group_by(FetchFileRecord.fetch_id, FetchFileRecord.collection_id)
        .order_by(FetchFileRecord.fetch_id, "first_order", FetchFileRecord.collection_id)
        .subquery()
    )
    aggregate = (
        func.json_group_array(ordered.c.collection_id)
        if session.get_bind().dialect.name == "sqlite"
        else func.json_agg(ordered.c.collection_id)
    )
    rows = session.execute(
        select(ordered.c.fetch_id, aggregate.label("collection_ids")).group_by(ordered.c.fetch_id)
    ).all()
    result: dict[str, list[str]] = {fetch_id: [] for fetch_id in fetch_ids}
    for row in rows:
        values = (
            json.loads(row.collection_ids)
            if isinstance(row.collection_ids, str)
            else row.collection_ids
        )
        result[row.fetch_id] = [str(value) for value in values]
    return result


def _fetch_summary(session: Session, fetch_id: str) -> FetchSummary:
    stmt, _ = _fetch_summary_query()
    row = session.execute(stmt.where(FetchRecord.fetch_id == fetch_id)).one_or_none()
    if row is None:
        raise NotFound(f"fetch not found: {fetch_id}")
    return _fetch_summary_from_row(row, _fetch_collection_ids(session, fetch_id))


def _fetch_summary_from_row(row: Any, collection_ids: Sequence[str]) -> FetchSummary:
    fetch = row[0]
    return FetchSummary(
        id=FetchId(fetch.fetch_id),
        name=fetch.name,
        collections=tuple(CollectionId(value) for value in collection_ids),
        state=FetchState(fetch.fetch_state),
        files=int(row.files),
        bytes=int(row.bytes),
        hot_files=int(row.hot_files),
        hot_bytes=int(row.hot_bytes),
        missing_files=int(row.missing_files),
        missing_bytes=int(row.missing_bytes),
    )


def _fetch_collection_ids(session: Session, fetch_id: str) -> list[str]:
    return _collections_by_fetch(session, [fetch_id]).get(fetch_id, [])


def _get_fetch(session: Session, fetch_id: str) -> FetchRecord:
    fetch = session.get(FetchRecord, fetch_id)
    if fetch is None:
        raise NotFound(f"fetch not found: {fetch_id}")
    return fetch


def _next_fetch_order(session: Session) -> int:
    return int(session.scalar(select(func.max(FetchRecord.fetch_order))) or 0) + 1


def _canonical_collection_ids(collections: Sequence[str]) -> list[str]:
    result: list[str] = []
    for value in collections:
        try:
            normalized = normalize_collection_id(value)
        except PathNormalizationError as exc:
            raise BadRequest(str(exc)) from exc
        if normalized not in result:
            result.append(normalized)
    return result


def _canonical_files(files: Sequence[FileSelection]) -> list[FileSelection]:
    result: list[FileSelection] = []
    for collection_id, path in files:
        try:
            normalized = (normalize_collection_id(collection_id), normalize_relpath(path))
        except PathNormalizationError as exc:
            raise BadRequest(str(exc)) from exc
        if normalized not in result:
            result.append(normalized)
    return result


def _require_collections_exist(session: Session, collection_ids: Sequence[str]) -> None:
    if not collection_ids:
        return
    existing = set(
        session.scalars(
            select(CollectionRecord.id).where(CollectionRecord.id.in_(collection_ids))
        ).all()
    )
    for collection_id in collection_ids:
        if collection_id not in existing:
            raise NotFound(f"collection not found: {collection_id}")


def _require_files_exist(session: Session, files: Sequence[FileSelection]) -> None:
    for collection_id, path in files:
        if session.get(CollectionFileRecord, (collection_id, path)) is None:
            raise NotFound(f"collection file not found: {collection_id}/{path}")


def _fetch_file_order_by(*, sort: str, order: str) -> tuple[Any, ...]:
    logical_path = CollectionFileRecord.collection_id + literal("/") + CollectionFileRecord.path
    primary = {
        "logical_path": logical_path,
        "collection_id": CollectionFileRecord.collection_id,
        "collection_path": CollectionFileRecord.path,
        "bytes": CollectionFileRecord.bytes,
        "hot": CollectionFileRecord.hot,
    }[sort]
    return (
        primary.desc() if order == "desc" else primary.asc(),
        CollectionFileRecord.collection_id,
        CollectionFileRecord.path,
    )


def _file_payload(file: CollectionFileRecord) -> dict[str, object]:
    return {
        "logical_path": f"{file.collection_id}/{file.path}",
        "collection_id": file.collection_id,
        "collection_path": file.path,
        "bytes": file.bytes,
        "sha256": file.sha256,
        "hot": file.hot,
    }


def _fetch_summary_payload(summary: FetchSummary) -> dict[str, object]:
    return {
        "id": str(summary.id),
        "name": summary.name,
        "collections": [str(value) for value in summary.collections],
        "state": summary.state.value,
        "files": summary.files,
        "bytes": summary.bytes,
        "hot_files": summary.hot_files,
        "hot_bytes": summary.hot_bytes,
        "missing_files": summary.missing_files,
        "missing_bytes": summary.missing_bytes,
    }


def _next_action(summary: FetchSummary) -> dict[str, str]:
    if summary.state == FetchState.DRAFT:
        return {"action": "start", "reason": "Review the selected files, then start the fetch."}
    if summary.state == FetchState.QUEUED_ARCHIVE:
        return {"action": "wait", "reason": "The required archive objects are queued."}
    if summary.state == FetchState.RESTORING_ARCHIVE:
        return {"action": "wait", "reason": "The required archive objects are being restored."}
    if summary.state == FetchState.DONE:
        return {"action": "none", "reason": "Every selected file is available in hot storage."}
    return {"action": "inspect", "reason": "Inspect the fetch and restore failure details."}


def _eviction_payload(
    *,
    collections: Sequence[str],
    files: Sequence[FileSelection],
    selected_files: int,
    selected_bytes: int,
    would_evict_files: int,
    would_evict_bytes: int,
    dry_run: bool,
) -> dict[str, object]:
    return {
        "collections": list(collections),
        "files": [{"collection_id": collection_id, "path": path} for collection_id, path in files],
        "dry_run": dry_run,
        "status": "planned" if dry_run else "evicted",
        "selected_files": selected_files,
        "selected_bytes": selected_bytes,
        "evicted_files": 0 if dry_run else would_evict_files,
        "evicted_bytes": 0 if dry_run else would_evict_bytes,
        "would_evict_files": would_evict_files,
        "would_evict_bytes": would_evict_bytes,
    }


def _normalize_fetch_name(name: str) -> str:
    normalized = " ".join(name.strip().split())
    if not normalized:
        raise BadRequest("fetch name is required")
    return normalized


def _require_editable(fetch: FetchRecord) -> None:
    if fetch.fetch_state != FetchState.DRAFT.value:
        raise InvalidState("fetch is already started and cannot be edited")


def _require_startable(fetch: FetchRecord) -> None:
    if fetch.fetch_state == FetchState.DONE.value:
        raise InvalidState("fetch is already complete")
    if fetch.fetch_state != FetchState.DRAFT.value:
        raise InvalidState("fetch is already started")


def _like_pattern(value: str) -> str:
    return f"%{value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')}%"

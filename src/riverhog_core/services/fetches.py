from __future__ import annotations

import math
from collections.abc import Callable, Sequence

from sqlalchemy import case, delete, func, or_, select
from sqlalchemy.orm import Session, selectinload

from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveRecord,
    CollectionFileRecord,
    CollectionRecord,
    FetchCollectionRecord,
    FetchRecord,
)
from riverhog_core.domain.enums import ArchiveState, FetchState
from riverhog_core.domain.errors import (
    BadRequest,
    Conflict,
    InvalidState,
    NotFound,
    ServiceUnavailable,
)
from riverhog_core.domain.models import FetchListPage, FetchSummary
from riverhog_core.domain.types import CollectionId, FetchId
from riverhog_core.fs_paths import PathNormalizationError, normalize_collection_id
from riverhog_core.ports.archive_store import (
    ArchiveObjectIdentity,
    ArchivePackageVerificationError,
    ArchiveStore,
    CollectionArchivePackageIdentity,
)
from riverhog_core.ports.hot_store import HotStore
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.collection_deletions import require_collection_not_deleting

_FETCH_SORT_FIELDS = {"id", "name", "state", "order", "files", "bytes", "missing_bytes"}
_FETCH_FILE_SORT_FIELDS = {
    "logical_path",
    "collection_id",
    "collection_path",
    "bytes",
    "hot",
}


class SqlAlchemyFetchService:
    def __init__(
        self,
        config: RuntimeConfig,
        archive_store: ArchiveStore,
        hot_store: HotStore,
    ) -> None:
        self._archive_store = archive_store
        self._hot_store = hot_store
        self._session_factory = make_session_factory(config.database_url)

    def create(
        self,
        *,
        name: str,
        collections: Sequence[str] | None = None,
    ) -> FetchSummary:
        normalized_name = _normalize_fetch_name(name)
        collection_ids = _canonical_collection_ids(collections or [])
        with session_scope(self._session_factory) as session:
            _require_collections_exist(session, collection_ids)
            fetch_order = _next_fetch_order(session)
            fetch = FetchRecord(
                fetch_id=f"fx-{fetch_order}",
                name=normalized_name,
                fetch_order=fetch_order,
                fetch_state=FetchState.DRAFT.value,
            )
            session.add(fetch)
            _replace_fetch_collections(session, fetch, collection_ids)
            session.flush()
            return _fetch_summary(session, fetch)

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

        stats = (
            select(
                FetchCollectionRecord.fetch_id.label("fetch_id"),
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
            .outerjoin(
                CollectionFileRecord,
                CollectionFileRecord.collection_id == FetchCollectionRecord.collection_id,
            )
            .group_by(FetchCollectionRecord.fetch_id)
            .subquery()
        )
        files_expr = func.coalesce(stats.c.files, 0)
        bytes_expr = func.coalesce(stats.c.bytes, 0)
        hot_files_expr = func.coalesce(stats.c.hot_files, 0)
        hot_bytes_expr = func.coalesce(stats.c.hot_bytes, 0)
        missing_bytes_expr = bytes_expr - hot_bytes_expr
        stmt = select(
            FetchRecord,
            files_expr,
            bytes_expr,
            hot_files_expr,
            hot_bytes_expr,
        ).outerjoin(stats, stats.c.fetch_id == FetchRecord.fetch_id)
        if state is not None:
            stmt = stmt.where(FetchRecord.fetch_state == state)
        if q:
            pattern = _like_pattern(q.casefold())
            collection_match = (
                select(FetchCollectionRecord.fetch_id)
                .where(
                    FetchCollectionRecord.fetch_id == FetchRecord.fetch_id,
                    func.lower(FetchCollectionRecord.collection_id).like(
                        pattern,
                        escape="\\",
                    ),
                )
                .exists()
            )
            stmt = stmt.where(
                or_(
                    func.lower(FetchRecord.fetch_id).like(pattern, escape="\\"),
                    func.lower(FetchRecord.name).like(pattern, escape="\\"),
                    collection_match,
                )
            )
        sort_expr = {
            "id": FetchRecord.fetch_id,
            "name": func.lower(FetchRecord.name),
            "state": FetchRecord.fetch_state,
            "order": FetchRecord.fetch_order,
            "files": files_expr,
            "bytes": bytes_expr,
            "missing_bytes": missing_bytes_expr,
        }[sort]
        order_expr = sort_expr.desc() if order == "desc" else sort_expr.asc()
        stmt = stmt.order_by(order_expr, FetchRecord.fetch_id.asc())

        with session_scope(self._session_factory) as session:
            total = int(session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
            selected_stmt = (
                stmt if all_items else stmt.offset((page - 1) * per_page).limit(per_page)
            )
            rows = session.execute(selected_stmt).all()
            fetch_ids = [row[0].fetch_id for row in rows]
            collections_by_fetch = _collections_by_fetch(session, fetch_ids)
            summaries = [
                FetchSummary(
                    id=FetchId(fetch.fetch_id),
                    name=fetch.name,
                    collections=tuple(
                        CollectionId(collection_id)
                        for collection_id in collections_by_fetch.get(fetch.fetch_id, [])
                    ),
                    state=FetchState(fetch.fetch_state),
                    files=int(files),
                    bytes=int(bytes_total),
                    hot_files=int(hot_files),
                    hot_bytes=int(hot_bytes),
                    missing_files=int(files) - int(hot_files),
                    missing_bytes=int(bytes_total) - int(hot_bytes),
                )
                for fetch, files, bytes_total, hot_files, hot_bytes in rows
            ]

        return FetchListPage(
            page=1 if all_items else page,
            per_page=total if all_items else per_page,
            total=total,
            pages=(1 if total else 0) if all_items else math.ceil(total / per_page) if total else 0,
            fetches=summaries,
        )

    def add_collections(
        self,
        fetch_id: str,
        collections: Sequence[str],
    ) -> FetchSummary:
        additions = _canonical_collection_ids(collections)
        if not additions:
            raise BadRequest("at least one collection is required")
        with session_scope(self._session_factory) as session:
            fetch = _get_fetch(session, fetch_id)
            _require_editable(fetch)
            _require_collections_exist(session, additions)
            existing = _fetch_collection_ids(session, fetch_id)
            _replace_fetch_collections(
                session,
                fetch,
                [*existing, *[item for item in additions if item not in existing]],
            )
            session.flush()
            return _fetch_summary(session, fetch)

    def remove_collections(
        self,
        fetch_id: str,
        collections: Sequence[str],
    ) -> FetchSummary:
        removals = set(_canonical_collection_ids(collections))
        if not removals:
            raise BadRequest("at least one collection is required")
        with session_scope(self._session_factory) as session:
            fetch = _get_fetch(session, fetch_id)
            _require_editable(fetch)
            remaining = [
                collection_id
                for collection_id in _fetch_collection_ids(session, fetch_id)
                if collection_id not in removals
            ]
            _replace_fetch_collections(session, fetch, remaining)
            session.flush()
            return _fetch_summary(session, fetch)

    def start(self, fetch_id: str) -> FetchSummary:
        with session_scope(self._session_factory) as session:
            fetch = _get_fetch(session, fetch_id)
            _require_startable(fetch)
            collection_ids = _fetch_collection_ids(session, fetch_id)
            if not collection_ids:
                raise InvalidState("fetch has no collections")
            for collection_id in collection_ids:
                require_collection_not_deleting(session, collection_id)
            files = _collection_files(session, collection_ids)
            if not files:
                raise InvalidState("fetch collections contain no files")
            fetch.fetch_state = (
                FetchState.DONE.value
                if all(file.hot for file in files)
                else FetchState.QUEUED_ARCHIVE.value
            )
            session.flush()
            return _fetch_summary(session, fetch)

    def cancel(self, fetch_id: str) -> FetchSummary:
        with session_scope(self._session_factory) as session:
            fetch = _get_fetch(session, fetch_id)
            if fetch.fetch_state == FetchState.DONE.value:
                raise InvalidState("completed fetches cannot be canceled")
            fetch.fetch_state = FetchState.DRAFT.value
            session.flush()
            return _fetch_summary(session, fetch)

    def evict(
        self,
        collections: Sequence[str],
        *,
        dry_run: bool = False,
    ) -> dict[str, object]:
        collection_ids = _canonical_collection_ids(collections)
        if not collection_ids:
            raise BadRequest("at least one collection is required")
        with session_scope(self._session_factory) as session:
            _require_collections_exist(session, collection_ids)
            selected = _collection_files(session, collection_ids)
            if not selected:
                raise NotFound("collections contain no files")
            packages: dict[str, CollectionArchivePackageIdentity] = {}
            for collection_id in collection_ids:
                package = _collection_archive_package_identity(session, collection_id)
                if package is None:
                    raise Conflict(
                        "cannot evict a collection before its archive upload is complete: "
                        f"{collection_id}"
                    )
                packages[collection_id] = package
            would_evict = [record for record in selected if record.hot]
            if dry_run:
                return _eviction_payload(
                    collections=collection_ids,
                    selected=selected,
                    affected=would_evict,
                    dry_run=True,
                )
            for collection_id, package in packages.items():
                try:
                    self._archive_store.verify_collection_archive_package(
                        collection_id=collection_id,
                        package=package,
                    )
                except ArchivePackageVerificationError as exc:
                    raise Conflict(
                        "cannot evict a collection because its remote archive does not match "
                        f"the upload record: {collection_id}"
                    ) from exc
                except Exception as exc:
                    raise ServiceUnavailable(
                        "cannot confirm the remote collection archive before hot eviction: "
                        f"{collection_id}"
                    ) from exc
            affected: list[CollectionFileRecord] = []
            for record in would_evict:
                try:
                    self._hot_store.delete_collection_file(record.collection_id, record.path)
                except FileNotFoundError:
                    pass
                record.hot = False
                affected.append(record)
            return _eviction_payload(
                collections=collection_ids,
                selected=selected,
                affected=affected,
                dry_run=False,
            )

    def get(self, fetch_id: str) -> FetchSummary:
        with session_scope(self._session_factory) as session:
            return _fetch_summary(session, _get_fetch(session, fetch_id))

    def status(self, fetch_id: str) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            fetch = _get_fetch(session, fetch_id)
            summary = _fetch_summary(session, fetch)
            files = _selected_files_for_fetch(session, fetch_id)
            collection_summaries = [
                {
                    "collection": collection_id,
                    **_collection_stats(session, collection_id),
                }
                for collection_id in _fetch_collection_ids(session, fetch_id)
            ]
            return {
                **_fetch_summary_payload(summary),
                "collection_summaries": collection_summaries,
                "files_preview": [_file_payload(file) for file in files[:25]],
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

        with session_scope(self._session_factory) as session:
            _get_fetch(session, fetch_id)
            files = _selected_files_for_fetch(session, fetch_id)
        if q:
            needle = q.casefold()
            files = [
                file for file in files if needle in f"{file.collection_id}/{file.path}".casefold()
            ]
        if hot is not None:
            files = [file for file in files if bool(file.hot) is hot]
        key: Callable[[CollectionFileRecord], str | int | bool] = {
            "logical_path": lambda file: f"{file.collection_id}/{file.path}",
            "collection_id": lambda file: file.collection_id,
            "collection_path": lambda file: file.path,
            "bytes": lambda file: file.bytes,
            "hot": lambda file: file.hot,
        }[sort]
        files.sort(
            key=lambda file: (key(file), file.collection_id, file.path),
            reverse=order == "desc",
        )
        total = len(files)
        start = (page - 1) * per_page
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
            "files": [_file_payload(file) for file in files[start : start + per_page]],
        }


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _get_fetch(session: Session, fetch_id: str) -> FetchRecord:
    fetch = session.scalar(
        select(FetchRecord)
        .options(selectinload(FetchRecord.collections))
        .where(FetchRecord.fetch_id == fetch_id)
    )
    if fetch is None:
        raise NotFound(f"fetch not found: {fetch_id}")
    return fetch


def _collection_archive_package_identity(
    session: Session,
    collection_id: str,
) -> CollectionArchivePackageIdentity | None:
    archive = session.get(CollectionArchiveRecord, collection_id)
    if (
        archive is None
        or archive.state != ArchiveState.UPLOADED.value
        or not archive.object_path
        or archive.stored_bytes is None
        or not archive.sha256
        or not archive.manifest_object_path
        or archive.manifest_stored_bytes is None
        or not archive.manifest_sha256
        or not archive.ots_object_path
        or archive.ots_stored_bytes is None
        or not archive.ots_sha256
        or not archive.last_verified_at
    ):
        return None
    return CollectionArchivePackageIdentity(
        archive=ArchiveObjectIdentity(
            object_path=archive.object_path,
            stored_bytes=archive.stored_bytes,
            sha256=archive.sha256,
        ),
        manifest=ArchiveObjectIdentity(
            object_path=archive.manifest_object_path,
            stored_bytes=archive.manifest_stored_bytes,
            sha256=archive.manifest_sha256,
        ),
        proof=ArchiveObjectIdentity(
            object_path=archive.ots_object_path,
            stored_bytes=archive.ots_stored_bytes,
            sha256=archive.ots_sha256,
        ),
    )


def _fetch_collection_records(
    session: Session,
    fetch_id: str,
) -> list[FetchCollectionRecord]:
    return list(
        session.scalars(
            select(FetchCollectionRecord)
            .where(FetchCollectionRecord.fetch_id == fetch_id)
            .order_by(
                FetchCollectionRecord.collection_order,
                FetchCollectionRecord.collection_id,
            )
        ).all()
    )


def _fetch_collection_ids(session: Session, fetch_id: str) -> list[str]:
    return [record.collection_id for record in _fetch_collection_records(session, fetch_id)]


def _collections_by_fetch(
    session: Session,
    fetch_ids: Sequence[str],
) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {fetch_id: [] for fetch_id in fetch_ids}
    if not fetch_ids:
        return result
    records = session.scalars(
        select(FetchCollectionRecord)
        .where(FetchCollectionRecord.fetch_id.in_(fetch_ids))
        .order_by(
            FetchCollectionRecord.fetch_id,
            FetchCollectionRecord.collection_order,
            FetchCollectionRecord.collection_id,
        )
    ).all()
    for record in records:
        result[record.fetch_id].append(record.collection_id)
    return result


def _collection_files(
    session: Session,
    collection_ids: Sequence[str],
) -> list[CollectionFileRecord]:
    if not collection_ids:
        return []
    return list(
        session.scalars(
            select(CollectionFileRecord)
            .where(CollectionFileRecord.collection_id.in_(collection_ids))
            .order_by(CollectionFileRecord.collection_id, CollectionFileRecord.path)
        ).all()
    )


def _selected_files_for_fetch(session: Session, fetch_id: str) -> list[CollectionFileRecord]:
    return _collection_files(session, _fetch_collection_ids(session, fetch_id))


def _fetch_summary(session: Session, fetch: FetchRecord) -> FetchSummary:
    collection_ids = _fetch_collection_ids(session, fetch.fetch_id)
    files = _collection_files(session, collection_ids)
    hot_files = [file for file in files if file.hot]
    return FetchSummary(
        id=FetchId(fetch.fetch_id),
        name=fetch.name,
        collections=tuple(CollectionId(collection_id) for collection_id in collection_ids),
        state=FetchState(fetch.fetch_state),
        files=len(files),
        bytes=sum(int(file.bytes) for file in files),
        hot_files=len(hot_files),
        hot_bytes=sum(int(file.bytes) for file in hot_files),
        missing_files=len(files) - len(hot_files),
        missing_bytes=sum(int(file.bytes) for file in files if not file.hot),
    )


def _replace_fetch_collections(
    session: Session,
    fetch: FetchRecord,
    collection_ids: Sequence[str],
) -> None:
    session.execute(
        delete(FetchCollectionRecord).where(FetchCollectionRecord.fetch_id == fetch.fetch_id)
    )
    for index, collection_id in enumerate(collection_ids, start=1):
        session.add(
            FetchCollectionRecord(
                fetch_id=fetch.fetch_id,
                collection_id=collection_id,
                collection_order=index,
            )
        )


def _normalize_fetch_name(name: str) -> str:
    normalized = " ".join(name.strip().split())
    if not normalized:
        raise BadRequest("fetch name is required")
    return normalized


def _canonical_collection_ids(collections: Sequence[str]) -> list[str]:
    canonical: list[str] = []
    seen: set[str] = set()
    for raw_collection in collections:
        try:
            collection_id = normalize_collection_id(raw_collection)
        except PathNormalizationError as exc:
            raise BadRequest(str(exc)) from exc
        if collection_id not in seen:
            seen.add(collection_id)
            canonical.append(collection_id)
    return canonical


def _require_collections_exist(session: Session, collection_ids: Sequence[str]) -> None:
    if not collection_ids:
        return
    existing = set(
        session.scalars(
            select(CollectionRecord.id).where(CollectionRecord.id.in_(collection_ids))
        ).all()
    )
    missing = [collection_id for collection_id in collection_ids if collection_id not in existing]
    if missing:
        raise NotFound(f"collection not found: {missing[0]}")


def _next_fetch_order(session: Session) -> int:
    return int(session.scalar(select(func.max(FetchRecord.fetch_order))) or 0) + 1


def _require_editable(fetch: FetchRecord) -> None:
    if fetch.fetch_state != FetchState.DRAFT.value:
        raise InvalidState("fetch is already started and cannot be edited")


def _require_startable(fetch: FetchRecord) -> None:
    if fetch.fetch_state == FetchState.DONE.value:
        raise InvalidState("fetch is already complete")
    if fetch.fetch_state != FetchState.DRAFT.value:
        raise InvalidState("fetch is already started")


def _collection_stats(session: Session, collection_id: str) -> dict[str, int]:
    files = _collection_files(session, [collection_id])
    hot_files = [file for file in files if file.hot]
    return {
        "files": len(files),
        "bytes": sum(int(file.bytes) for file in files),
        "hot_files": len(hot_files),
        "hot_bytes": sum(int(file.bytes) for file in hot_files),
        "missing_files": len(files) - len(hot_files),
        "missing_bytes": sum(int(file.bytes) for file in files if not file.hot),
    }


def _file_payload(file: CollectionFileRecord) -> dict[str, object]:
    return {
        "logical_path": f"{file.collection_id}/{file.path}",
        "collection_id": file.collection_id,
        "collection_path": file.path,
        "bytes": int(file.bytes),
        "sha256": file.sha256,
        "hot": bool(file.hot),
    }


def _fetch_summary_payload(summary: FetchSummary) -> dict[str, object]:
    return {
        "id": str(summary.id),
        "name": summary.name,
        "collections": [str(collection) for collection in summary.collections],
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
        return {"action": "start", "reason": "restore missing collections when needed"}
    if summary.state in {FetchState.QUEUED_ARCHIVE, FetchState.RESTORING_ARCHIVE}:
        return {"action": "wait", "reason": "collection restoration is in progress"}
    if summary.state == FetchState.FAILED:
        return {"action": "inspect", "reason": "collection restoration failed"}
    return {"action": "none", "reason": "all collections are hot"}


def _eviction_payload(
    *,
    collections: list[str],
    selected: list[CollectionFileRecord],
    affected: list[CollectionFileRecord],
    dry_run: bool,
) -> dict[str, object]:
    return {
        "collections": collections,
        "dry_run": dry_run,
        "status": "would_evict" if dry_run else "evicted",
        "files": len(selected),
        "bytes": sum(int(record.bytes) for record in selected),
        "evicted_files": 0 if dry_run else len(affected),
        "evicted_bytes": 0 if dry_run else sum(int(record.bytes) for record in affected),
        "would_evict_files": len(affected),
        "would_evict_bytes": sum(int(record.bytes) for record in affected),
    }

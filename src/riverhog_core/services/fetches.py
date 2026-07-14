from __future__ import annotations

import math
from collections.abc import Callable, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, selectinload

from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveRecord,
    CollectionFileRecord,
    FetchRecord,
    FetchSelectorRecord,
)
from riverhog_core.domain.enums import ArchiveState, FetchState
from riverhog_core.domain.errors import BadRequest, Conflict, InvalidState, NotFound
from riverhog_core.domain.models import FetchListPage, FetchSummary
from riverhog_core.domain.selectors import parse_target
from riverhog_core.domain.types import FetchId, TargetStr
from riverhog_core.ports.hot_store import HotStore
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.collection_deletions import require_collection_not_deleting
from riverhog_core.services.target_selection import selected_collection_files

_FETCH_SORT_FIELDS = {"id", "name", "state", "order", "files", "bytes", "missing_bytes"}
_FETCH_FILE_SORT_FIELDS = {"target", "collection", "path", "bytes", "hot"}


class SqlAlchemyFetchService:
    def __init__(self, config: RuntimeConfig, hot_store: HotStore) -> None:
        self._hot_store = hot_store
        self._session_factory = make_session_factory(config.database_url)

    def create(self, *, name: str, targets: Sequence[str] | None = None) -> FetchSummary:
        normalized_name = _normalize_fetch_name(name)
        canonical_targets = _canonical_targets(targets or [])
        with session_scope(self._session_factory) as session:
            fetch_order = _next_fetch_order(session)
            fetch = FetchRecord(
                fetch_id=f"fx-{fetch_order}",
                name=normalized_name,
                fetch_order=fetch_order,
                fetch_state=FetchState.DRAFT.value,
            )
            session.add(fetch)
            _replace_fetch_selectors(session, fetch, canonical_targets)
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

        with session_scope(self._session_factory) as session:
            records = list(
                session.scalars(
                    select(FetchRecord)
                    .options(selectinload(FetchRecord.selectors))
                    .order_by(FetchRecord.fetch_order, FetchRecord.fetch_id)
                ).all()
            )
            summaries = [_fetch_summary(session, record) for record in records]

        if state is not None:
            summaries = [summary for summary in summaries if summary.state.value == state]
        if q:
            needle = q.casefold()
            summaries = [
                summary
                for summary in summaries
                if needle in str(summary.id).casefold()
                or needle in summary.name.casefold()
                or any(needle in str(target).casefold() for target in summary.targets)
            ]
        key: Callable[[FetchSummary], str | int] = {
            "id": lambda item: str(item.id),
            "name": lambda item: item.name.casefold(),
            "state": lambda item: item.state.value,
            "order": lambda item: int(str(item.id).split("-", 1)[-1]),
            "files": lambda item: item.files,
            "bytes": lambda item: item.bytes,
            "missing_bytes": lambda item: item.missing_bytes,
        }[sort]
        summaries.sort(key=lambda item: (key(item), str(item.id)), reverse=order == "desc")
        total = len(summaries)
        start = (page - 1) * per_page
        return FetchListPage(
            page=page,
            per_page=per_page,
            total=total,
            pages=math.ceil(total / per_page) if total else 0,
            fetches=summaries[start : start + per_page],
        )

    def add_targets(self, fetch_id: str, targets: Sequence[str]) -> FetchSummary:
        additions = _canonical_targets(targets)
        if not additions:
            raise BadRequest("at least one target is required")
        with session_scope(self._session_factory) as session:
            fetch = _get_fetch(session, fetch_id)
            _require_editable(fetch)
            existing = [selector.target for selector in _fetch_selectors(session, fetch_id)]
            _replace_fetch_selectors(
                session,
                fetch,
                [*existing, *[target for target in additions if target not in existing]],
            )
            session.flush()
            return _fetch_summary(session, fetch)

    def remove_targets(self, fetch_id: str, targets: Sequence[str]) -> FetchSummary:
        removals = set(_canonical_targets(targets))
        if not removals:
            raise BadRequest("at least one target is required")
        with session_scope(self._session_factory) as session:
            fetch = _get_fetch(session, fetch_id)
            _require_editable(fetch)
            remaining = [
                selector.target
                for selector in _fetch_selectors(session, fetch_id)
                if selector.target not in removals
            ]
            _replace_fetch_selectors(session, fetch, remaining)
            session.flush()
            return _fetch_summary(session, fetch)

    def start(self, fetch_id: str) -> FetchSummary:
        with session_scope(self._session_factory) as session:
            fetch = _get_fetch(session, fetch_id)
            _require_startable(fetch)
            files = _selected_files_for_fetch(session, fetch_id)
            if not files:
                raise InvalidState("fetch has no matching files")
            for collection_id in sorted({file.collection_id for file in files}):
                require_collection_not_deleting(session, collection_id)
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

    def evict(self, targets: Sequence[str], *, dry_run: bool = False) -> dict[str, object]:
        canonical_targets = _canonical_targets(targets)
        if not canonical_targets:
            raise BadRequest("at least one target is required")
        with session_scope(self._session_factory) as session:
            selected_by_key: dict[tuple[str, str], CollectionFileRecord] = {}
            for target in canonical_targets:
                for record in selected_collection_files(session, target):
                    selected_by_key[(record.collection_id, record.path)] = record
            selected = [selected_by_key[key] for key in sorted(selected_by_key)]
            if not selected:
                raise NotFound("target selectors matched no files")
            unsafe = [
                record
                for record in selected
                if not _collection_archive_is_verified(session, record.collection_id)
            ]
            if unsafe:
                first = unsafe[0]
                raise Conflict(
                    "cannot evict hot file before its collection archive is verified: "
                    f"{first.collection_id}/{first.path}"
                )
            would_evict = [record for record in selected if record.hot]
            if dry_run:
                return _eviction_payload(
                    targets=canonical_targets,
                    selected=selected,
                    affected=would_evict,
                    dry_run=True,
                )
            affected: list[CollectionFileRecord] = []
            for record in would_evict:
                try:
                    self._hot_store.delete_collection_file(record.collection_id, record.path)
                except FileNotFoundError:
                    pass
                record.hot = False
                affected.append(record)
            return _eviction_payload(
                targets=canonical_targets,
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
            files = _selected_files_for_fetch(session, fetch_id, missing_ok=True)
            target_summaries = [
                {
                    "target": selector.target,
                    **_target_stats(session, selector.target),
                }
                for selector in _fetch_selectors(session, fetch_id)
            ]
            return {
                **_fetch_summary_payload(summary),
                "target_summaries": target_summaries,
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
            raise BadRequest(
                f"sort must be one of {', '.join(sorted(_FETCH_FILE_SORT_FIELDS))}"
            )
        if order not in {"asc", "desc"}:
            raise BadRequest("order must be asc or desc")

        with session_scope(self._session_factory) as session:
            _get_fetch(session, fetch_id)
            files = _selected_files_for_fetch(session, fetch_id, missing_ok=True)
        if q:
            needle = q.casefold()
            files = [
                file
                for file in files
                if needle in f"{file.collection_id}/{file.path}".casefold()
            ]
        if hot is not None:
            files = [file for file in files if bool(file.hot) is hot]
        key: Callable[[CollectionFileRecord], str | int | bool] = {
            "target": lambda file: f"{file.collection_id}/{file.path}",
            "collection": lambda file: file.collection_id,
            "path": lambda file: file.path,
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


def _get_fetch(session: Session, fetch_id: str) -> FetchRecord:
    fetch = session.scalar(
        select(FetchRecord)
        .options(selectinload(FetchRecord.selectors))
        .where(FetchRecord.fetch_id == fetch_id)
    )
    if fetch is None:
        raise NotFound(f"fetch not found: {fetch_id}")
    return fetch


def _collection_archive_is_verified(session: Session, collection_id: str) -> bool:
    archive = session.get(CollectionArchiveRecord, collection_id)
    return bool(
        archive is not None
        and archive.state == ArchiveState.UPLOADED.value
        and archive.object_path
        and archive.sha256
        and archive.last_verified_at
    )


def _fetch_selectors(session: Session, fetch_id: str) -> list[FetchSelectorRecord]:
    return list(
        session.scalars(
            select(FetchSelectorRecord)
            .where(FetchSelectorRecord.fetch_id == fetch_id)
            .order_by(FetchSelectorRecord.selector_order, FetchSelectorRecord.target)
        ).all()
    )


def _selected_files_for_fetch(
    session: Session,
    fetch_id: str,
    *,
    missing_ok: bool = False,
) -> list[CollectionFileRecord]:
    selected: dict[tuple[str, str], CollectionFileRecord] = {}
    selectors = _fetch_selectors(session, fetch_id)
    for selector in selectors:
        for file in selected_collection_files(
            session,
            selector.target,
            missing_ok=True,
        ):
            selected[(file.collection_id, file.path)] = file
    files = [selected[key] for key in sorted(selected)]
    if not files and not missing_ok:
        raise NotFound(f"fetch targets matched no files: {fetch_id}")
    return files


def _fetch_summary(session: Session, fetch: FetchRecord) -> FetchSummary:
    files = _selected_files_for_fetch(session, fetch.fetch_id, missing_ok=True)
    hot_files = [file for file in files if file.hot]
    return FetchSummary(
        id=FetchId(fetch.fetch_id),
        name=fetch.name,
        targets=tuple(
            TargetStr(selector.target) for selector in _fetch_selectors(session, fetch.fetch_id)
        ),
        state=FetchState(fetch.fetch_state),
        files=len(files),
        bytes=sum(int(file.bytes) for file in files),
        hot_files=len(hot_files),
        hot_bytes=sum(int(file.bytes) for file in hot_files),
        missing_files=len(files) - len(hot_files),
        missing_bytes=sum(int(file.bytes) for file in files if not file.hot),
    )


def _replace_fetch_selectors(
    session: Session,
    fetch: FetchRecord,
    targets: Sequence[str],
) -> None:
    session.execute(
        delete(FetchSelectorRecord).where(FetchSelectorRecord.fetch_id == fetch.fetch_id)
    )
    for index, target in enumerate(targets, start=1):
        session.add(
            FetchSelectorRecord(
                fetch_id=fetch.fetch_id,
                target=target,
                selector_order=index,
            )
        )


def _normalize_fetch_name(name: str) -> str:
    normalized = " ".join(name.strip().split())
    if not normalized:
        raise BadRequest("fetch name is required")
    return normalized


def _canonical_targets(targets: Sequence[str]) -> list[str]:
    canonical: list[str] = []
    seen: set[str] = set()
    for raw_target in targets:
        target = parse_target(raw_target)
        if target.canonical not in seen:
            seen.add(target.canonical)
            canonical.append(target.canonical)
    return canonical


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


def _target_stats(session: Session, target: str) -> dict[str, int]:
    files = selected_collection_files(session, target, missing_ok=True)
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
        "target": f"{file.collection_id}/{file.path}",
        "collection": file.collection_id,
        "path": file.path,
        "bytes": int(file.bytes),
        "sha256": file.sha256,
        "hot": bool(file.hot),
    }


def _fetch_summary_payload(summary: FetchSummary) -> dict[str, object]:
    return {
        "id": str(summary.id),
        "name": summary.name,
        "targets": [str(target) for target in summary.targets],
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
        return {"action": "start", "reason": "start archive materialization when needed"}
    if summary.state in {FetchState.QUEUED_ARCHIVE, FetchState.RESTORING_ARCHIVE}:
        return {"action": "wait", "reason": "archive materialization is in progress"}
    if summary.state == FetchState.FAILED:
        return {"action": "inspect", "reason": "archive materialization failed"}
    return {"action": "none", "reason": "selected files are hot"}


def _eviction_payload(
    *,
    targets: list[str],
    selected: list[CollectionFileRecord],
    affected: list[CollectionFileRecord],
    dry_run: bool,
) -> dict[str, object]:
    return {
        "targets": targets,
        "dry_run": dry_run,
        "status": "would_evict" if dry_run else "evicted",
        "files": len(selected),
        "bytes": sum(int(record.bytes) for record in selected),
        "evicted_files": 0 if dry_run else len(affected),
        "evicted_bytes": 0 if dry_run else sum(int(record.bytes) for record in affected),
        "would_evict_files": len(affected),
        "would_evict_bytes": sum(int(record.bytes) for record in affected),
    }

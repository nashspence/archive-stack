from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from riverhog_core.catalog_models import CollectionFileRecord, CollectionRecord
from riverhog_core.domain.errors import NotFound
from riverhog_core.domain.selectors import parse_target


@dataclass(frozen=True)
class SelectedCollectionFileStats:
    files: int
    bytes: int
    hot_files: int
    hot_bytes: int

    @property
    def missing_files(self) -> int:
        return self.files - self.hot_files

    @property
    def missing_bytes(self) -> int:
        return self.bytes - self.hot_bytes


def _target_file_clauses(session: Session, raw_target: str) -> list[ColumnElement[bool]]:
    target = parse_target(raw_target)
    collection_ids = session.scalars(select(CollectionRecord.id)).all()
    clauses: list[ColumnElement[bool]] = []
    for collection_id in collection_ids:
        collection_prefix = f"{collection_id}/"
        if target.is_dir:
            if collection_prefix.startswith(target.canonical):
                clauses.append(CollectionFileRecord.collection_id == collection_id)
            elif target.canonical.startswith(collection_prefix):
                rel_prefix = target.canonical[len(collection_prefix) :]
                clauses.append(
                    and_(
                        CollectionFileRecord.collection_id == collection_id,
                        CollectionFileRecord.path.startswith(rel_prefix),
                    )
                )
        elif target.canonical.startswith(collection_prefix):
            rel_path = target.canonical[len(collection_prefix) :]
            if rel_path:
                clauses.append(
                    and_(
                        CollectionFileRecord.collection_id == collection_id,
                        CollectionFileRecord.path == rel_path,
                    )
                )
    return clauses


def selected_collection_files(
    session: Session,
    raw_target: str,
    *,
    missing_ok: bool = False,
) -> list[CollectionFileRecord]:
    clauses = _target_file_clauses(session, raw_target)
    if not clauses:
        if missing_ok:
            return []
        raise NotFound(f"target not found: {raw_target}")

    stmt = (
        select(CollectionFileRecord)
        .where(or_(*clauses))
        .order_by(CollectionFileRecord.collection_id, CollectionFileRecord.path)
    )
    selected = list(session.scalars(stmt).all())
    if not selected and not missing_ok:
        raise NotFound(f"target not found: {raw_target}")
    return selected


def selected_collection_file_stats(
    session: Session,
    raw_target: str,
    *,
    missing_ok: bool = False,
) -> SelectedCollectionFileStats:
    clauses = _target_file_clauses(session, raw_target)
    if not clauses:
        if missing_ok:
            return SelectedCollectionFileStats(files=0, bytes=0, hot_files=0, hot_bytes=0)
        raise NotFound(f"target not found: {raw_target}")

    files, bytes_total, hot_files, hot_bytes = session.execute(
        select(
            func.count(),
            func.coalesce(func.sum(CollectionFileRecord.bytes), 0),
            func.coalesce(
                func.sum(
                    case(
                        (CollectionFileRecord.hot.is_(True), 1),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(
                func.sum(
                    case(
                        (CollectionFileRecord.hot.is_(True), CollectionFileRecord.bytes),
                        else_=0,
                    )
                ),
                0,
            ),
        ).where(or_(*clauses))
    ).one()
    stats = SelectedCollectionFileStats(
        files=int(files or 0),
        bytes=int(bytes_total or 0),
        hot_files=int(hot_files or 0),
        hot_bytes=int(hot_bytes or 0),
    )
    if stats.files == 0 and not missing_ok:
        raise NotFound(f"target not found: {raw_target}")
    return stats

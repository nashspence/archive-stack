from __future__ import annotations

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, selectinload

from riverhog_core.catalog_models import CollectionFileRecord, CollectionRecord
from riverhog_core.domain.errors import NotFound
from riverhog_core.domain.selectors import parse_target


def selected_collection_files(
    session: Session,
    raw_target: str,
    *,
    load_copies: bool = False,
    missing_ok: bool = False,
) -> list[CollectionFileRecord]:
    target = parse_target(raw_target)
    collection_ids = session.scalars(select(CollectionRecord.id)).all()
    clauses = []
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

    if not clauses:
        if missing_ok:
            return []
        raise NotFound(f"target not found: {raw_target}")

    stmt = (
        select(CollectionFileRecord)
        .where(or_(*clauses))
        .order_by(CollectionFileRecord.collection_id, CollectionFileRecord.path)
    )
    if load_copies:
        stmt = stmt.options(selectinload(CollectionFileRecord.copies))
    selected = list(session.scalars(stmt).all())
    if not selected and not missing_ok:
        raise NotFound(f"target not found: {raw_target}")
    return selected

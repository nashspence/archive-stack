"""Transactional projections derived from immutable collection/tag membership."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import update
from sqlalchemy.orm import Session

from riverhog_core.catalog_models import TagRecord


def adjust_tag_collection_counts(
    session: Session,
    *,
    added: Iterable[str] = (),
    removed: Iterable[str] = (),
) -> None:
    """Apply one collection-membership delta without materializing tag relations."""

    added_ids = tuple(sorted(set(added)))
    removed_ids = tuple(sorted(set(removed)))
    if set(added_ids) & set(removed_ids):
        raise RuntimeError("tag projection delta overlaps")
    if added_ids:
        result = session.execute(
            update(TagRecord)
            .where(TagRecord.id.in_(added_ids))
            .values(collection_count=TagRecord.collection_count + 1)
        )
        if int(getattr(result, "rowcount", 0) or 0) != len(added_ids):
            raise RuntimeError("tag projection target disappeared")
    if removed_ids:
        result = session.execute(
            update(TagRecord)
            .where(TagRecord.id.in_(removed_ids), TagRecord.collection_count > 0)
            .values(collection_count=TagRecord.collection_count - 1)
        )
        if int(getattr(result, "rowcount", 0) or 0) != len(removed_ids):
            raise RuntimeError("tag projection is inconsistent")


__all__ = ["adjust_tag_collection_counts"]

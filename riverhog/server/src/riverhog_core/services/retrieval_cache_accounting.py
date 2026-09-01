from __future__ import annotations

from sqlalchemy.orm import Session
from time_formats import format_utc_timestamp, utc_now

from riverhog_core.catalog_models import RetrievalCacheStoreAccountingRecord


def adjust_cache_committed_bytes(
    accounting: RetrievalCacheStoreAccountingRecord,
    *,
    delta: int,
) -> None:
    updated = accounting.committed_bytes + delta
    if updated < 0:
        raise RuntimeError("retrieval cache committed accounting is inconsistent")
    accounting.committed_bytes = updated
    accounting.generation += 1
    accounting.updated_at = format_utc_timestamp(utc_now())


def locked_cache_accounting(
    session: Session,
    cache_store: str,
) -> RetrievalCacheStoreAccountingRecord:
    accounting = session.get(
        RetrievalCacheStoreAccountingRecord,
        cache_store,
        with_for_update=True,
    )
    if accounting is None:
        raise RuntimeError(f"retrieval cache accounting is unavailable: {cache_store}")
    return accounting


__all__ = ["adjust_cache_committed_bytes", "locked_cache_accounting"]

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetrievalCacheReceipt:
    cache_store: str
    object_path: str
    revision: str | None
    stored_bytes: int
    stored_sha256: str | None
    cached_at: str
    verified_at: str

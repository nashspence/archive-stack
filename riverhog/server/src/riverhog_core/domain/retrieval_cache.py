from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetrievalCacheReceipt:
    object_path: str
    revision: str
    stored_bytes: int
    stored_sha256: str
    cached_at: str
    verified_at: str

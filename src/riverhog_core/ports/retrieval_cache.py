from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RetrievalCacheReceipt:
    object_path: str
    version_id: str | None
    stored_bytes: int
    stored_sha256: str
    cached_at: str
    verified_at: str


class RetrievalCache(Protocol):
    def put(
        self,
        *,
        source_store: str,
        collection_id: str,
        object_id: str,
        content: Iterable[bytes],
        content_length: int,
    ) -> RetrievalCacheReceipt: ...

    def iter_object(
        self,
        *,
        object_path: str,
        version_id: str | None,
        expected_bytes: int,
        expected_sha256: str,
    ) -> Iterator[bytes]: ...

    def delete(self, *, object_path: str, version_id: str | None) -> None: ...

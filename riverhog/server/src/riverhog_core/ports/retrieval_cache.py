from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from riverhog_core.domain.retrieval_cache import RetrievalCacheReceipt as RetrievalCacheReceipt

if TYPE_CHECKING:
    from riverhog_core.ports.archive_objects import (
        ArchiveResumableObjectStore,
    )


@dataclass(frozen=True, slots=True)
class RetrievalCacheAdmission:
    owner: str
    cache_store: str
    source_store: str
    collection_id: int
    object_id: str
    object_path: str
    expected_bytes: int
    write_token: str | None
    admitted_at: str
    completed: RetrievalCacheReceipt | None = None


class RetrievalCache(Protocol):
    def abort_incomplete_writes(
        self,
        *,
        initiated_before: datetime,
    ) -> int: ...

    def admit(
        self,
        *,
        owner: str,
        source_store: str,
        collection_id: int,
        object_id: str,
        expected_bytes: int,
    ) -> RetrievalCacheAdmission | None: ...

    def release(self, *, owner: str) -> int: ...

    def is_current(self, *, admission: RetrievalCacheAdmission) -> bool: ...

    def reap_abandoned_populations(self, *, limit: int = 100) -> int: ...

    def put(
        self,
        *,
        admission: RetrievalCacheAdmission,
        content: Iterable[bytes],
    ) -> RetrievalCacheReceipt: ...

    def iter_object(
        self,
        *,
        cache_store: str,
        object_path: str,
        revision: str | None,
        expected_bytes: int,
        expected_sha256: str,
    ) -> Iterator[bytes]: ...

    def iter_object_range(
        self,
        *,
        cache_store: str,
        object_path: str,
        revision: str | None,
        expected_bytes: int,
        offset: int,
        size: int,
    ) -> Iterator[bytes]: ...

    def delete(
        self,
        *,
        cache_store: str,
        object_path: str,
        revision: str | None,
    ) -> None: ...

    def resumable_object_store(
        self,
        *,
        admission: RetrievalCacheAdmission,
    ) -> ArchiveResumableObjectStore: ...

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from riverhog_core.domain.retrieval_cache import RetrievalCacheReceipt as RetrievalCacheReceipt

if TYPE_CHECKING:
    from riverhog_core.ports.archive_objects import (
        ArchiveMultipartObjectStore,
        CompletedObjectReceipt,
        MultipartPartReceipt,
    )


class RetrievalCache(Protocol):
    def abort_incomplete_multipart_uploads(
        self,
        *,
        initiated_before: datetime,
    ) -> int: ...

    def put(
        self,
        *,
        source_store: str,
        collection_id: int,
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

    def iter_object_range(
        self,
        *,
        object_path: str,
        version_id: str | None,
        expected_bytes: int,
        offset: int,
        size: int,
    ) -> Iterator[bytes]: ...

    def delete(self, *, object_path: str, version_id: str | None) -> None: ...

    def multipart_object_store(
        self,
        *,
        source_store: str,
        collection_id: int,
        object_id: str,
    ) -> ArchiveMultipartObjectStore: ...

    def verify_multipart_object(
        self,
        *,
        completed: CompletedObjectReceipt,
        parts: tuple[MultipartPartReceipt, ...] = (),
    ) -> RetrievalCacheReceipt: ...

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Protocol

from riverhog_core.ports.archive_store import ArchiveMultipartUploadTracker


@dataclass(frozen=True)
class ProtectionMirrorArchiveStat:
    bytes: int
    sha256: str | None = None


class ProtectionMirrorStore(Protocol):
    def object_path(self, collection_id: str) -> str: ...

    def put_collection_archive_stream_resumable(
        self,
        collection_id: str,
        chunks: Iterable[bytes],
        *,
        content_length: int,
        sha256: str,
        multipart_tracker: ArchiveMultipartUploadTracker | None = None,
    ) -> None: ...

    def iter_collection_archive(
        self,
        collection_id: str,
        *,
        offset: int = 0,
        size: int | None = None,
    ) -> Iterator[bytes]: ...

    def stat_collection_archive(
        self,
        collection_id: str,
    ) -> ProtectionMirrorArchiveStat | None: ...

    def delete_collection(self, collection_id: str) -> None: ...

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class HotFileStat:
    bytes: int
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class HotCollectionFile:
    path: str
    bytes: int


@dataclass(frozen=True, slots=True)
class HotCollectionListing:
    files: tuple[HotCollectionFile, ...]
    file_count: int
    total_bytes: int


class HotStore(Protocol):
    def put_collection_file(self, collection_id: str, path: str, content: bytes) -> None: ...
    def put_collection_file_stream(
        self,
        collection_id: str,
        path: str,
        chunks: Iterable[bytes],
        *,
        content_length: int,
        sha256: str | None = None,
    ) -> None: ...
    def get_collection_file(self, collection_id: str, path: str) -> bytes: ...
    def iter_collection_file(
        self,
        collection_id: str,
        path: str,
        *,
        offset: int = 0,
        size: int | None = None,
    ) -> Iterator[bytes]: ...
    def stat_collection_file(self, collection_id: str, path: str) -> HotFileStat | None: ...
    def has_collection_file(self, collection_id: str, path: str) -> bool: ...
    def delete_collection_file(self, collection_id: str, path: str) -> None: ...
    def list_collection_files(self, collection_id: str) -> HotCollectionListing: ...

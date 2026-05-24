from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Protocol


class HotStore(Protocol):
    def put_collection_file(self, collection_id: str, path: str, content: bytes) -> None: ...
    def put_collection_file_stream(
        self,
        collection_id: str,
        path: str,
        chunks: Iterable[bytes],
        *,
        content_length: int,
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
    def has_collection_file(self, collection_id: str, path: str) -> bool: ...
    def delete_collection_file(self, collection_id: str, path: str) -> None: ...
    def list_collection_files(self, collection_id: str) -> list[tuple[str, int]]: ...

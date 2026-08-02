from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field


def iter_chunk_range(chunks: Iterable[bytes], offset: int, length: int) -> Iterator[bytes]:
    remaining_offset = offset
    remaining_length = length
    for chunk in chunks:
        if not chunk:
            continue
        if remaining_offset >= len(chunk):
            remaining_offset -= len(chunk)
            continue
        current = chunk[remaining_offset:]
        remaining_offset = 0
        if len(current) > remaining_length:
            current = current[:remaining_length]
        if current:
            remaining_length -= len(current)
            yield current
        if remaining_length == 0:
            return
    if remaining_offset or remaining_length:
        raise ValueError("collection archive source ended before requested range")


@dataclass(frozen=True, slots=True)
class CollectionArchiveSourceFile:
    path: str
    content: bytes
    sha256: str

    @property
    def bytes(self) -> int:
        return len(self.content)


@dataclass(frozen=True, slots=True)
class CollectionArchiveFile:
    path: str
    bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ArchiveObjectPlacement:
    path: str
    file_offset: int
    bytes: int
    member: str | None = None


@dataclass(frozen=True, slots=True)
class CollectionArchiveDataObject:
    object_id: str
    kind: str
    plaintext_bytes: int
    sha256: str
    placements: tuple[ArchiveObjectPlacement, ...]
    _chunks: Callable[[], Iterator[bytes]] = field(repr=False, compare=False)
    _chunks_range: Callable[[int, int], Iterator[bytes]] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def iter_plaintext(self) -> Iterator[bytes]:
        yield from self._chunks()

    def iter_plaintext_range(self, offset: int, size: int) -> Iterator[bytes]:
        if offset < 0:
            raise ValueError("archive object offset must be non-negative")
        if size < 0:
            raise ValueError("archive object range size must be non-negative")
        if offset + size > self.plaintext_bytes:
            raise ValueError("archive object range exceeds its plaintext size")
        if self._chunks_range is not None:
            yield from self._chunks_range(offset, size)
            return
        yield from iter_chunk_range(self.iter_plaintext(), offset, size)

    @property
    def supports_ranges(self) -> bool:
        return self._chunks_range is not None


@dataclass(frozen=True, slots=True)
class CollectionArchive:
    collection_id: int
    files: tuple[CollectionArchiveFile, ...]
    data_objects: tuple[CollectionArchiveDataObject, ...]
    manifest_bytes: bytes
    manifest_sha256: str
    proof_bytes: bytes
    proof_sha256: str

    def require_object(self, object_id: str) -> CollectionArchiveDataObject:
        for current in self.data_objects:
            if current.object_id == object_id:
                return current
        raise KeyError(object_id)

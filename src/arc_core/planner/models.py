from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class PlannerConfig:
    target_bytes: int
    fill_bytes: int
    meta_pad_bytes: int = 2048


@dataclass(frozen=True)
class CollectionArtifact:
    source: Path
    container_relpath: str
    encrypted_size: int


@dataclass(frozen=True)
class PlannerPiece:
    collection: str
    file_id: int | str
    relpath: str
    store_relpath: str
    payload_bytes: int
    piece_index: int
    piece_count: int
    estimated_on_disc_bytes: int


@dataclass(frozen=True)
class PlannerFile:
    file_id: int | str
    relpath: str
    source: Path
    plaintext_bytes: int
    mode: int | None
    mtime: float | int | None
    uid: int | None
    gid: int | None
    sha256: str
    pieces: list[PlannerPiece] = field(default_factory=list)

    @property
    def piece_count(self) -> int:
        return len(self.pieces)


@dataclass(frozen=True)
class PlannedItem:
    item_id: str
    collection: str
    kind: str
    priority: bool
    reason: str
    pieces: list[PlannerPiece]
    planned_bytes: int


@dataclass(frozen=True)
class PlannerCollection:
    collection_id: str
    files: list[PlannerFile]
    fixed_bytes: int = 0
    artifacts: list[CollectionArtifact] = field(default_factory=list)

    @property
    def payload_bytes(self) -> int:
        return sum(file.plaintext_bytes for file in self.files)

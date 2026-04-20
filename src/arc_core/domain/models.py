from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from arc_core.domain.enums import FetchState
from arc_core.domain.types import CollectionId, CopyId, FetchId, ImageId, Sha256Hex, TargetStr


@dataclass(frozen=True)
class Target:
    collection_id: CollectionId
    path: PurePosixPath | None
    is_dir: bool

    @property
    def is_collection(self) -> bool:
        return self.path is None

    @property
    def canonical(self) -> str:
        if self.path is None:
            return str(self.collection_id)
        suffix = str(self.path)
        if self.is_dir and not suffix.endswith("/"):
            suffix += "/"
        return f"{self.collection_id}:{suffix}"


@dataclass(frozen=True)
class CollectionSummary:
    id: CollectionId
    files: int
    bytes: int
    hot_bytes: int
    archived_bytes: int

    @property
    def pending_bytes(self) -> int:
        return self.bytes - self.archived_bytes


@dataclass(frozen=True)
class ImageSummary:
    id: ImageId
    bytes: int
    fill: float
    files: int
    collections: int
    iso_ready: bool


@dataclass(frozen=True)
class CopySummary:
    id: CopyId
    image: ImageId
    location: str
    created_at: str


@dataclass(frozen=True)
class FetchCopyHint:
    id: CopyId
    location: str


@dataclass(frozen=True)
class FetchSummary:
    id: FetchId
    target: TargetStr
    state: FetchState
    files: int
    bytes: int
    copies: list[FetchCopyHint]


@dataclass(frozen=True)
class PinSummary:
    target: TargetStr


@dataclass(frozen=True)
class FileRef:
    collection_id: CollectionId
    path: str
    bytes: int
    sha256: Sha256Hex
    copies: list[FetchCopyHint]

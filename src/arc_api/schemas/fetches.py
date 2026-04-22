from __future__ import annotations

from pydantic import Field

from arc_api.schemas.common import ArcModel
from arc_api.schemas.pins import FetchHintCopyOut, HotStatusOut


class FetchSummaryOut(ArcModel):
    id: str
    target: str
    state: str
    files: int
    bytes: int
    copies: list[FetchHintCopyOut]


class FetchManifestCopyOut(ArcModel):
    copy_: str = Field(alias="copy")
    volume_id: str
    location: str
    disc_path: str
    enc: dict


class FetchManifestPartOut(ArcModel):
    index: int
    bytes: int
    sha256: str
    copies: list[FetchManifestCopyOut]


class FetchManifestEntryOut(ArcModel):
    id: str
    path: str
    bytes: int
    sha256: str
    copies: list[FetchManifestCopyOut]
    parts: list[FetchManifestPartOut]


class FetchManifestResponse(ArcModel):
    id: str
    target: str
    entries: list[FetchManifestEntryOut]


class UploadEntryResponse(ArcModel):
    entry: str
    accepted: bool
    bytes: int


class CompleteFetchResponse(ArcModel):
    id: str
    state: str
    hot: HotStatusOut

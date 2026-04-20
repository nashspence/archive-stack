from __future__ import annotations

from pydantic import Field

from arc_api.schemas.common import ArcModel


class ImageSummaryResponse(ArcModel):
    id: str
    bytes: int
    fill: float
    iso_ready: bool
    files: int
    collections: list[str]


class RegisterCopyRequest(ArcModel):
    id: str
    location: str


class CopyOut(ArcModel):
    id: str
    image: str
    location: str
    created_at: str


class RegisterCopyResponse(ArcModel):
    copy_: CopyOut = Field(alias="copy")

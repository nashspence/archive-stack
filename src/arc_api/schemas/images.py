from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, Field

from arc_api.schemas.archive import GlacierArchiveOut
from arc_api.schemas.common import ArcModel


class FinalizedImageSummaryResponse(ArcModel):
    id: str
    filename: str
    finalized_at: str
    bytes: int
    fill: float
    files: int
    collections: int
    collection_ids: list[str]
    iso_ready: Literal[True] = True
    protection_state: Literal["unprotected", "partially_protected", "protected"]
    physical_copies_required: int
    physical_copies_registered: int
    physical_copies_verified: int
    physical_copies_missing: int
    glacier: GlacierArchiveOut


class ListImagesResponse(ArcModel):
    page: int
    per_page: int
    total: int
    pages: int
    sort: Literal["finalized_at", "bytes", "physical_copies_registered"]
    order: Literal["asc", "desc"]
    images: list[FinalizedImageSummaryResponse]


class RegisterCopyRequest(ArcModel):
    copy_id: str | None = Field(default=None, validation_alias=AliasChoices("copy_id", "id"))
    location: str


class UpdateCopyRequest(ArcModel):
    location: str | None = None
    state: (
        Literal["needed", "burning", "verified", "registered", "lost", "damaged", "retired"] | None
    ) = None
    verification_state: Literal["pending", "verified", "failed"] | None = None


class CopyHistoryOut(ArcModel):
    at: str
    event: str
    state: Literal["needed", "burning", "verified", "registered", "lost", "damaged", "retired"]
    verification_state: Literal["pending", "verified", "failed"]
    location: str | None


class CopyOut(ArcModel):
    id: str
    volume_id: str
    label_text: str
    location: str | None
    created_at: str
    state: Literal["needed", "burning", "verified", "registered", "lost", "damaged", "retired"]
    verification_state: Literal["pending", "verified", "failed"]
    history: list[CopyHistoryOut]


class RegisterCopyResponse(ArcModel):
    copy_: CopyOut = Field(alias="copy")


class ListCopiesResponse(ArcModel):
    copies: list[CopyOut]

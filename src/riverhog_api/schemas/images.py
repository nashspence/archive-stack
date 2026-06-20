from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, Field

from riverhog_api.schemas.common import RiverhogModel


class FinalizedImageSummaryResponse(RiverhogModel):
    id: str
    filename: str
    finalized_at: str
    bytes: int
    target_bytes: int
    fill: float
    files: int
    collections: int
    collection_ids: list[str]
    iso_ready: Literal[True] = True
    physical_protection_state: Literal["unprotected", "partially_protected", "protected"]
    physical_copies_required: int
    physical_copies_registered: int
    physical_copies_verified: int
    physical_copies_missing: int


class ListImagesResponse(RiverhogModel):
    page: int
    per_page: int
    total: int
    pages: int
    sort: Literal["finalized_at", "bytes", "physical_copies_registered"]
    order: Literal["asc", "desc"]
    images: list[FinalizedImageSummaryResponse]


class RegisterCopyRequest(RiverhogModel):
    copy_id: str | None = Field(default=None, validation_alias=AliasChoices("copy_id", "id"))
    location: str


class UpdateCopyRequest(RiverhogModel):
    location: str | None = None
    state: (
        Literal["needed", "burning", "verified", "registered", "lost", "damaged", "retired"] | None
    ) = None
    verification_state: Literal["pending", "verified", "failed"] | None = None


class CopyHistoryOut(RiverhogModel):
    at: str
    event: str
    state: Literal["needed", "burning", "verified", "registered", "lost", "damaged", "retired"]
    verification_state: Literal["pending", "verified", "failed"]
    location: str | None


class CopyOut(RiverhogModel):
    id: str
    volume_id: str
    label_text: str
    location: str | None
    created_at: str
    state: Literal["needed", "burning", "verified", "registered", "lost", "damaged", "retired"]
    verification_state: Literal["pending", "verified", "failed"]
    history: list[CopyHistoryOut]


class DiscOut(CopyOut):
    image_id: str
    filename: str | None = None


class RegisterCopyResponse(RiverhogModel):
    copy_: CopyOut = Field(alias="copy")


class ListCopiesResponse(RiverhogModel):
    copies: list[CopyOut]


class ListDiscsResponse(RiverhogModel):
    page: int
    per_page: int
    total: int
    pages: int
    sort: Literal["id", "image_id", "state", "verification_state", "location"]
    order: Literal["asc", "desc"]
    query: str | None
    image_id: str | None = None
    discs: list[DiscOut]

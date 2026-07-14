from __future__ import annotations

from typing import Literal

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
    disc_redundancy_state: Literal["none", "partial", "full"]
    discs_required: int
    discs_registered: int
    discs_verified: int
    discs_missing: int


class ListImagesResponse(RiverhogModel):
    page: int
    per_page: int
    total: int
    pages: int
    sort: Literal["finalized_at", "bytes", "discs_registered"]
    order: Literal["asc", "desc"]
    images: list[FinalizedImageSummaryResponse]


class RegisterDiscRequest(RiverhogModel):
    disc_id: str | None = None
    location: str


class UpdateDiscRequest(RiverhogModel):
    location: str | None = None
    state: (
        Literal["needed", "burning", "verified", "registered", "lost", "damaged", "retired"] | None
    ) = None
    verification_state: Literal["pending", "verified", "failed"] | None = None


class DiscHistoryOut(RiverhogModel):
    at: str
    event: str
    state: Literal["needed", "burning", "verified", "registered", "lost", "damaged", "retired"]
    verification_state: Literal["pending", "verified", "failed"]
    location: str | None


class DiscOut(RiverhogModel):
    disc_id: str
    image_id: str
    label_text: str
    location: str | None
    created_at: str
    state: Literal["needed", "burning", "verified", "registered", "lost", "damaged", "retired"]
    verification_state: Literal["pending", "verified", "failed"]
    history: list[DiscHistoryOut]
    filename: str | None = None


class RegisterDiscResponse(RiverhogModel):
    disc: DiscOut


class ImageDiscsResponse(RiverhogModel):
    discs: list[DiscOut]


class ListDiscsResponse(RiverhogModel):
    page: int
    per_page: int
    total: int
    pages: int
    sort: Literal["disc_id", "image_id", "state", "verification_state", "location"]
    order: Literal["asc", "desc"]
    query: str | None
    image_id: str | None = None
    discs: list[DiscOut]

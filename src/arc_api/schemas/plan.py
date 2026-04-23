from __future__ import annotations

from typing import Literal

from arc_api.schemas.common import ArcModel


class PlanCandidateResponse(ArcModel):
    candidate_id: str
    bytes: int
    fill: float
    collections: int
    collection_ids: list[str]
    files: int
    iso_ready: bool


class PlanResponse(ArcModel):
    page: int
    per_page: int
    total: int
    pages: int
    sort: Literal["fill", "bytes", "files", "collections", "candidate_id"]
    order: Literal["asc", "desc"]
    ready: bool
    target_bytes: int
    min_fill_bytes: int
    candidates: list[PlanCandidateResponse]
    unplanned_bytes: int
    note: str | None = None

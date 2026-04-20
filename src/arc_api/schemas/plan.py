from __future__ import annotations

from arc_api.schemas.common import ArcModel


class PlanImageOut(ArcModel):
    id: str
    bytes: int
    fill: float
    collections: int
    files: int
    iso_ready: bool


class PlanResponse(ArcModel):
    ready: bool
    target_bytes: int
    min_fill_bytes: int
    images: list[PlanImageOut]
    unplanned_bytes: int
    note: str | None = None

from __future__ import annotations

from arc_api.schemas.common import ArcModel


class CloseCollectionRequest(ArcModel):
    path: str


class CollectionSummaryOut(ArcModel):
    id: str
    files: int
    bytes: int
    hot_bytes: int
    archived_bytes: int
    pending_bytes: int


class CloseCollectionResponse(ArcModel):
    collection: CollectionSummaryOut

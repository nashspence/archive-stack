from __future__ import annotations

from arc_api.schemas.common import ArcModel


class SearchCopyOut(ArcModel):
    id: str
    volume_id: str
    location: str


class SearchResultOut(ArcModel):
    kind: str
    target: str
    collection: str
    path: str | None = None
    bytes: int | None = None
    hot: bool | None = None
    files: int | None = None
    hot_bytes: int | None = None
    archived_bytes: int | None = None
    pending_bytes: int | None = None
    copies: list[SearchCopyOut] = []


class SearchResponse(ArcModel):
    query: str
    results: list[SearchResultOut]

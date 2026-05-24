from __future__ import annotations

from riverhog_api.schemas.common import RiverhogModel


class SearchCopyOut(RiverhogModel):
    id: str
    volume_id: str
    location: str


class SearchResultOut(RiverhogModel):
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


class SearchResponse(RiverhogModel):
    query: str
    results: list[SearchResultOut]

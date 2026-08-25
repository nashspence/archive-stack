from __future__ import annotations

from riverhog_protocol import CollectionId, SearchSort, SortOrder

from riverhog_api.schemas.common import RiverhogModel


class SearchFileOut(RiverhogModel):
    file_ref: str
    collection_id: CollectionId
    path: str
    bytes: int
    sha256: str


class SearchResponse(RiverhogModel):
    query: str | None
    collection: CollectionId | None
    page: int
    per_page: int
    total: int
    pages: int
    sort: SearchSort
    order: SortOrder
    files: list[SearchFileOut]

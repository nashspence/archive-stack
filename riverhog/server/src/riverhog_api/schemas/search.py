from __future__ import annotations

from riverhog_protocol import SearchSort, SortOrder

from riverhog_api.schemas.common import RiverhogModel


class SearchFileOut(RiverhogModel):
    file_ref: str
    collection_id: int
    path: str
    bytes: int
    sha256: str


class SearchResponse(RiverhogModel):
    query: str | None
    collection: int | None
    page: int
    per_page: int
    total: int
    pages: int
    sort: SearchSort
    order: SortOrder
    files: list[SearchFileOut]

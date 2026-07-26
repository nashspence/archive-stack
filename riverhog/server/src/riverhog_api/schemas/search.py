from __future__ import annotations

from typing import Literal

from riverhog_api.schemas.common import RiverhogModel


class SearchFileOut(RiverhogModel):
    logical_path: str
    collection_id: int
    collection_path: str
    bytes: int
    sha256: str


class SearchResponse(RiverhogModel):
    query: str | None
    collection: int | None
    page: int
    per_page: int
    total: int
    pages: int
    sort: Literal["logical_path", "collection_id", "collection_path", "bytes"]
    order: Literal["asc", "desc"]
    files: list[SearchFileOut]

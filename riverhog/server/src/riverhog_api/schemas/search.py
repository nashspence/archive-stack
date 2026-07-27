from __future__ import annotations

from typing import Literal

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
    sort: Literal["file_ref", "collection_id", "path", "bytes"]
    order: Literal["asc", "desc"]
    files: list[SearchFileOut]

from __future__ import annotations

from typing import Literal

from riverhog_api.schemas.common import RiverhogModel


class SearchFileOut(RiverhogModel):
    logical_path: str
    collection_id: str
    collection_path: str
    bytes: int
    sha256: str
    hot: bool


class SearchResponse(RiverhogModel):
    query: str | None
    collection: str | None
    hot: bool | None
    page: int
    per_page: int
    total: int
    pages: int
    sort: Literal["logical_path", "collection_id", "collection_path", "bytes", "hot"]
    order: Literal["asc", "desc"]
    files: list[SearchFileOut]

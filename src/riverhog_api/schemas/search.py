from __future__ import annotations

from typing import Literal

from riverhog_api.schemas.common import RiverhogModel


class SearchFileOut(RiverhogModel):
    target: str
    collection: str
    path: str
    bytes: int
    sha256: str
    hot: bool
    archived: bool


class SearchResponse(RiverhogModel):
    query: str | None
    collection: str | None
    hot: bool | None
    archived: bool | None
    page: int
    per_page: int
    total: int
    pages: int
    sort: Literal["target", "collection", "path", "bytes", "hot", "archived"]
    order: Literal["asc", "desc"]
    files: list[SearchFileOut]

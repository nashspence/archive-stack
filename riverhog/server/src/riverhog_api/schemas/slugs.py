from __future__ import annotations

from typing import Literal

from riverhog_api.schemas.common import RiverhogModel


class CreateSlugRequest(RiverhogModel):
    id: str


class SlugOut(RiverhogModel):
    id: str
    created_by_app: str
    created_by_key_id: str | None
    created_at: str
    collections: int
    uploads: int


class SlugListOut(RiverhogModel):
    page: int
    per_page: int
    total: int
    pages: int
    sort: str
    order: Literal["asc", "desc"]
    query: str | None
    slugs: list[SlugOut]

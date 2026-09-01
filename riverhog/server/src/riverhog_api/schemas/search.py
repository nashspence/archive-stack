from __future__ import annotations

from http_api_contracts import BrowsePageToken
from pydantic import Field, model_validator
from riverhog_protocol import (
    CollectionId,
    ImmutableFileIdentityDocument,
    SearchSort,
    SortOrder,
)

from riverhog_api.schemas.common import RiverhogModel


class SearchFileOut(ImmutableFileIdentityDocument):
    file_ref: str
    collection_id: CollectionId

    @model_validator(mode="after")
    def validate_file_ref(self) -> SearchFileOut:
        if self.file_ref != f"{self.collection_id}/{self.path}":
            raise ValueError("file_ref must match the exact collection file identity")
        return self


class SearchResponse(RiverhogModel):
    query: str | None
    collection: CollectionId | None
    page_size: int = Field(ge=1, le=100)
    next_page_token: BrowsePageToken | None
    sort: SearchSort
    order: SortOrder
    files: list[SearchFileOut]

from __future__ import annotations

from http_api_contracts import BrowsePageToken
from pydantic import Field
from riverhog_protocol import (
    CollectionAccessGroupSort,
    CollectionAccessGroupStatus,
    CollectionId,
    SortOrder,
)

from riverhog_api.schemas.common import RiverhogModel


class CollectionAccessGroupCreateIn(RiverhogModel):
    idempotency_key: str = Field(min_length=1, max_length=300)
    display_label: str | None = Field(default=None, min_length=1, max_length=300)


class CollectionAccessGroupUpdateIn(RiverhogModel):
    display_label: str | None = Field(min_length=1, max_length=300)
    status: CollectionAccessGroupStatus


class CollectionAccessGroupOut(RiverhogModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    display_label: str | None
    status: CollectionAccessGroupStatus
    authorization_revision: int = Field(ge=1, strict=True)
    collection_count: int = Field(ge=0, strict=True)
    created_by_app: str
    created_by_key_id: str | None
    created_at: str
    updated_at: str


class CollectionAccessGroupListOut(RiverhogModel):
    page_size: int = Field(ge=1, le=100, strict=True)
    next_page_token: BrowsePageToken | None
    query: str | None
    status: CollectionAccessGroupStatus | None
    sort: CollectionAccessGroupSort
    order: SortOrder
    groups: list[CollectionAccessGroupOut]


class CollectionAccessGroupMemberOut(RiverhogModel):
    collection_id: CollectionId
    added_by_app: str
    added_by_key_id: str | None
    added_at: str


class CollectionAccessGroupMembersOut(RiverhogModel):
    group_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_revision: int = Field(ge=1, strict=True)
    page_size: int = Field(ge=1, le=100, strict=True)
    next_page_token: BrowsePageToken | None
    members: list[CollectionAccessGroupMemberOut]


class CollectionAccessGroupsForCollectionOut(RiverhogModel):
    collection_id: CollectionId
    page_size: int = Field(ge=1, le=100, strict=True)
    next_page_token: BrowsePageToken | None
    groups: list[CollectionAccessGroupOut]


class CollectionAccessGroupMembershipOut(RiverhogModel):
    group_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    collection_id: CollectionId
    present: bool
    changed: bool
    authorization_revision: int = Field(ge=1, strict=True)
    collection_count: int = Field(ge=0, strict=True)


__all__ = [
    "CollectionAccessGroupCreateIn",
    "CollectionAccessGroupListOut",
    "CollectionAccessGroupMembershipOut",
    "CollectionAccessGroupMembersOut",
    "CollectionAccessGroupOut",
    "CollectionAccessGroupUpdateIn",
    "CollectionAccessGroupsForCollectionOut",
]

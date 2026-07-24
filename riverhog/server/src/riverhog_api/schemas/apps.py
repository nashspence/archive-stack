from __future__ import annotations

from typing import Literal

from pydantic import Field

from riverhog_api.schemas.common import RiverhogModel


class AppSummaryOut(RiverhogModel):
    name: str
    keys: int
    active_keys: int
    last_used_at: str | None


class AppListOut(RiverhogModel):
    page: int
    per_page: int
    total: int
    pages: int
    sort: str
    order: Literal["asc", "desc"]
    query: str | None
    active: bool | None
    apps: list[AppSummaryOut]


class AppKeyOut(RiverhogModel):
    id: str
    app: str
    permissions: list[str]
    collection_grants: list[str]
    monthly_download_quota_bytes: int | None
    status: Literal["active", "expired", "revoked"]
    created_at: str
    expires_at: str | None
    revoked_at: str | None
    last_used_at: str | None


class AppKeyCreatedOut(AppKeyOut):
    token: str


class AppKeyListOut(RiverhogModel):
    page: int
    per_page: int
    total: int
    pages: int
    sort: str
    order: Literal["asc", "desc"]
    query: str | None
    active: bool | None
    app: str
    keys: list[AppKeyOut]


class CreateAppKeyRequest(RiverhogModel):
    permissions: list[str] = Field(min_length=1)
    collection_grants: list[str] = Field(default_factory=list)
    expires_in_seconds: int | None = Field(default=None, ge=1)


class ReplaceCollectionGrantsRequest(RiverhogModel):
    collection_grants: list[str]


class CollectionGrantOut(RiverhogModel):
    id: str
    created_at: str


class CollectionGrantListOut(RiverhogModel):
    page: int
    per_page: int
    total: int
    pages: int
    sort: str
    order: Literal["asc", "desc"]
    query: str | None
    app: str
    key_id: str
    grants: list[CollectionGrantOut]


class CollectionGrantSetOut(RiverhogModel):
    app: str
    key_id: str
    collection_grants: list[str]

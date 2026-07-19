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
    expires_in_seconds: int | None = Field(default=None, ge=1)

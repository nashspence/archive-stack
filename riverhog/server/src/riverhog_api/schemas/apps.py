from __future__ import annotations

from typing import Literal

from application_access import (
    ApplicationAccessGrant,
    ApplicationAccessGrantSet,
    ApplicationKeyId,
    ApplicationName,
    ApplicationPermission,
    ApplicationResource,
)
from pydantic import Field
from riverhog_protocol import (
    ApplicationAccessSort,
    ApplicationKeySort,
    ApplicationSort,
    SortOrder,
)

from riverhog_api.schemas.common import RiverhogModel


class AppSummaryOut(RiverhogModel):
    name: ApplicationName
    keys: int
    active_keys: int
    last_used_at: str | None


class AppListOut(RiverhogModel):
    page: int
    per_page: int
    total: int
    pages: int
    sort: ApplicationSort
    order: SortOrder
    query: str | None
    active: bool | None
    apps: list[AppSummaryOut]


class AppAccessIn(ApplicationAccessGrant):
    pass


class AppAccessOut(AppAccessIn):
    pass


class AppAccessListItemOut(AppAccessOut):
    app: ApplicationName
    key_id: ApplicationKeyId
    key_status: Literal["active", "expired", "revoked"]
    created_at: str


class AppAccessListFiltersOut(RiverhogModel):
    app: ApplicationName | None
    key_id: ApplicationKeyId | None
    permission: ApplicationPermission | None
    resource: ApplicationResource | None
    active: bool | None


class AppKeyOut(RiverhogModel):
    id: ApplicationKeyId
    app: ApplicationName
    access: list[AppAccessOut]
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
    sort: ApplicationKeySort
    order: SortOrder
    query: str | None
    active: bool | None
    app: ApplicationName
    keys: list[AppKeyOut]


class CreateAppKeyRequest(RiverhogModel):
    access: ApplicationAccessGrantSet
    expires_in_seconds: int | None = Field(default=None, ge=1)


class ReplaceAppAccessRequest(RiverhogModel):
    access: ApplicationAccessGrantSet


class MutateAppAccessRequest(AppAccessIn):
    pass


class AppAccessListOut(RiverhogModel):
    page: int
    per_page: int
    total: int
    pages: int
    sort: ApplicationAccessSort
    order: SortOrder
    query: str | None
    filters: AppAccessListFiltersOut
    access: list[AppAccessListItemOut]


class AppAccessSetOut(RiverhogModel):
    app: ApplicationName
    key_id: ApplicationKeyId
    access: list[AppAccessOut]

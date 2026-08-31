from __future__ import annotations

from typing import Literal, Self

from pydantic import ConfigDict, Field, model_validator
from riverhog_application_access import (
    ApplicationAccessGrant,
    ApplicationAccessGrantSet,
    ApplicationKeyId,
    ApplicationName,
    ApplicationPermission,
    ApplicationResource,
    MonthlyDownloadQuotaBytes,
)
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
    page_size: int = Field(ge=1, le=100)
    next_page_token: str | None
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
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"status": {"const": "revoked"}}},
                    "then": {"properties": {"revoked_at": {"type": "string"}}},
                    "else": {"properties": {"revoked_at": {"type": "null"}}},
                },
                {
                    "if": {"properties": {"status": {"const": "expired"}}},
                    "then": {"properties": {"expires_at": {"type": "string"}}},
                },
            ]
        }
    )

    id: ApplicationKeyId
    app: ApplicationName
    access: ApplicationAccessGrantSet
    monthly_download_quota_bytes: MonthlyDownloadQuotaBytes | None
    status: Literal["active", "expired", "revoked"]
    created_at: str
    expires_at: str | None
    revoked_at: str | None
    last_used_at: str | None

    @model_validator(mode="after")
    def validate_status_evidence(self) -> Self:
        if (self.revoked_at is not None) != (self.status == "revoked"):
            raise ValueError("application-key revoked_at must match revoked state")
        if self.status == "expired" and self.expires_at is None:
            raise ValueError("expired application keys require expires_at")
        return self


class AppKeyCreatedOut(AppKeyOut):
    token: str


class AppKeyListOut(RiverhogModel):
    page_size: int = Field(ge=1, le=100)
    next_page_token: str | None
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
    page_size: int = Field(ge=1, le=100)
    next_page_token: str | None
    sort: ApplicationAccessSort
    order: SortOrder
    query: str | None
    filters: AppAccessListFiltersOut
    access: list[AppAccessListItemOut]


class AppAccessSetOut(RiverhogModel):
    app: ApplicationName
    key_id: ApplicationKeyId
    access: ApplicationAccessGrantSet

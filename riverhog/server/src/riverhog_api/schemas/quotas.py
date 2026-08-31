from __future__ import annotations

from typing import Literal

from pydantic import Field
from riverhog_application_access import ApplicationKeyId, ApplicationName, MonthlyDownloadQuotaBytes
from riverhog_protocol import DownloadQuotaSort, SortOrder

from riverhog_api.schemas.common import RiverhogModel


class KeyDownloadQuotaOut(RiverhogModel):
    id: str
    app: ApplicationName
    key_id: ApplicationKeyId
    key_status: Literal["active", "expired", "revoked"]
    monthly_bytes: MonthlyDownloadQuotaBytes | None
    month_started_at: str
    resets_at: str
    accounted_bytes: int = Field(ge=0)
    reserved_bytes: int = Field(ge=0)
    remaining_bytes: int | None = Field(ge=0)


class KeyDownloadQuotaListOut(RiverhogModel):
    page_size: int = Field(ge=1, le=100)
    next_page_token: str | None
    sort: DownloadQuotaSort
    order: SortOrder
    query: str | None
    app: ApplicationName | None
    active: bool | None
    quotas: list[KeyDownloadQuotaOut]


class SetKeyDownloadQuotaRequest(RiverhogModel):
    monthly_bytes: MonthlyDownloadQuotaBytes | None

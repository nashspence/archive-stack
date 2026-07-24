from __future__ import annotations

from typing import Literal

from riverhog_api.schemas.common import RiverhogModel


class KeyDownloadQuotaOut(RiverhogModel):
    id: str
    app: str
    key_id: str
    key_status: Literal["active", "expired", "revoked"]
    monthly_bytes: int | None
    month_started_at: str
    resets_at: str
    accounted_bytes: int
    reserved_bytes: int
    remaining_bytes: int | None


class KeyDownloadQuotaListOut(RiverhogModel):
    page: int
    per_page: int
    total: int
    pages: int
    sort: str
    order: Literal["asc", "desc"]
    query: str | None
    app: str | None
    active: bool | None
    quotas: list[KeyDownloadQuotaOut]


class SetKeyDownloadQuotaRequest(RiverhogModel):
    monthly_bytes: int | None

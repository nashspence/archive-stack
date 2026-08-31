from __future__ import annotations

from typing import Literal

from pydantic import Field
from riverhog_protocol import ArchiveStoreName, ArchiveStoreSort, SortOrder

from riverhog_api.schemas.common import RiverhogModel


class ArchiveDownloadAllowanceOut(RiverhogModel):
    store: ArchiveStoreName
    state: Literal["open", "closed"]
    month_started_at: str
    resets_at: str
    allowance_bytes: int
    safety_buffer_bytes: int
    effective_limit_bytes: int
    accounted_bytes: int
    reserved_bytes: int
    remaining_bytes: int


class ArchiveStoreOut(RiverhogModel):
    store: ArchiveStoreName
    read_mode: Literal["immediate", "restore_required"]
    read_priority: int
    write_target: bool
    collections: int
    objects: int
    stored_bytes: int
    download_allowance: ArchiveDownloadAllowanceOut | None


class ArchiveStoreListOut(RiverhogModel):
    page_size: int = Field(ge=1, le=100)
    next_page_token: str | None
    sort: ArchiveStoreSort
    order: SortOrder
    query: str | None
    stores: list[ArchiveStoreOut]

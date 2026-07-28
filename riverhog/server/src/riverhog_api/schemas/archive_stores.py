from __future__ import annotations

from typing import Literal

from riverhog_api.schemas.common import RiverhogModel


class ArchiveDownloadAllowanceOut(RiverhogModel):
    store: str
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
    store: str
    backend: Literal["aws", "b2", "s3"]
    storage_class: str
    read_mode: Literal["immediate", "restore_required"]
    write_target: bool
    collections: int
    objects: int
    stored_bytes: int
    download_allowance: ArchiveDownloadAllowanceOut | None


class ArchiveStoreListOut(RiverhogModel):
    page: int
    per_page: int
    total: int
    pages: int
    sort: str
    order: Literal["asc", "desc"]
    query: str | None
    stores: list[ArchiveStoreOut]

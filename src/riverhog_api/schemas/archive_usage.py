from __future__ import annotations

from typing import Literal

from riverhog_api.schemas.archive import ArchiveCopyOut
from riverhog_api.schemas.common import RiverhogModel


class ArchiveUsageTotalsOut(RiverhogModel):
    collections: int
    uploaded_collections: int
    measured_storage_bytes: int


class ArchiveUsageCollectionOut(RiverhogModel):
    id: str
    bytes: int
    archive_copies: list[ArchiveCopyOut]
    measured_storage_bytes: int


class ArchiveUsageSnapshotOut(RiverhogModel):
    captured_at: str
    uploaded_collections: int
    measured_storage_bytes: int


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


class ArchiveUsageReportOut(RiverhogModel):
    scope: Literal["all", "collection"]
    measured_at: str
    totals: ArchiveUsageTotalsOut
    collections: list[ArchiveUsageCollectionOut]
    history: list[ArchiveUsageSnapshotOut]
    download_allowances: list[ArchiveDownloadAllowanceOut]

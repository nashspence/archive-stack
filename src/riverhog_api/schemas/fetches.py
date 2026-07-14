from __future__ import annotations

from typing import Literal

from pydantic import Field

from riverhog_api.schemas.common import RiverhogModel
from riverhog_api.schemas.recovery_sessions import RecoverySessionListOut


class HotStatusOut(RiverhogModel):
    state: str
    present_bytes: int
    missing_bytes: int


class FetchCopyHintOut(RiverhogModel):
    id: str
    volume_id: str
    location: str


class FetchSummaryOut(RiverhogModel):
    id: str
    name: str
    targets: list[str]
    state: str
    files: int
    bytes: int
    entries_total: int
    entries_pending: int
    entries_partial: int
    entries_byte_complete: int
    entries_uploaded: int
    uploaded_bytes: int
    missing_bytes: int
    upload_state_expires_at: str | None
    copies: list[FetchCopyHintOut]


class FetchesResponse(RiverhogModel):
    page: int
    per_page: int
    total: int
    pages: int
    fetches: list[FetchSummaryOut]


class CreateFetchRequest(RiverhogModel):
    name: str
    targets: list[str] = Field(default_factory=list)


class FetchTargetsRequest(RiverhogModel):
    targets: list[str]


class StartFetchRequest(RiverhogModel):
    cloud: bool = False
    dry_run: bool = False


class FetchStartPlanOut(FetchSummaryOut):
    dry_run: bool
    status: str
    cloud: bool
    queued_state: str
    will_create_recovery_session: bool


class HotEvictRequest(RiverhogModel):
    targets: list[str]
    dry_run: bool = False


class HotEvictResponse(RiverhogModel):
    targets: list[str]
    dry_run: bool = False
    status: str
    files: int
    bytes: int
    evicted_files: int
    evicted_bytes: int
    would_evict_files: int
    would_evict_bytes: int


class FetchStatusEntryOut(RiverhogModel):
    id: str
    collection_id: str
    path: str
    bytes: int
    upload_state: str
    uploaded_bytes: int
    upload_state_expires_at: str | None


class FetchTargetSummaryOut(RiverhogModel):
    target: str
    files: int
    bytes: int
    hot_files: int
    hot_bytes: int
    archived_files: int
    archived_bytes: int
    registered_disc_files: int
    missing_files: int
    missing_with_disc_files: int
    missing_without_disc_files: int


class FetchFileOut(RiverhogModel):
    target: str
    collection_id: str
    path: str
    bytes: int
    hot: bool
    archived: bool
    registered_disc_coverage: bool


class FetchStatusResponse(FetchSummaryOut):
    hot_files: int
    hot_bytes: int
    archived_files: int
    archived_bytes: int
    registered_disc_files: int
    missing_files: int
    missing_with_disc_files: int
    missing_without_disc_files: int
    target_summaries: list[FetchTargetSummaryOut]
    files_preview_limit: int
    files_preview_returned: int
    files_preview: list[FetchFileOut]
    next_action: str
    next_action_reason: str
    cloud_fetch: RecoverySessionListOut
    entries_limit: int
    entries_returned: int
    entries: list[FetchStatusEntryOut]


class FetchFilesResponse(RiverhogModel):
    fetch_id: str
    query: str | None
    hot: bool | None
    archived: bool | None
    disc_coverage: bool | None
    page: int
    per_page: int
    total: int
    pages: int
    sort: Literal["target", "collection", "path", "bytes", "hot", "archived", "disc"]
    order: Literal["asc", "desc"]
    files: list[FetchFileOut]


class FetchManifestCopyOut(RiverhogModel):
    copy_: str = Field(alias="copy")
    volume_id: str
    location: str
    disc_path: str
    recovery_bytes: int
    recovery_sha256: str


class FetchManifestPartOut(RiverhogModel):
    index: int
    bytes: int
    sha256: str
    recovery_bytes: int
    copies: list[FetchManifestCopyOut]


class FetchManifestEntryOut(RiverhogModel):
    id: str
    collection_id: str
    path: str
    bytes: int
    sha256: str
    recovery_bytes: int
    upload_state: str
    uploaded_bytes: int
    upload_state_expires_at: str | None
    copies: list[FetchManifestCopyOut]
    parts: list[FetchManifestPartOut]


class FetchManifestResponse(RiverhogModel):
    id: str
    name: str
    targets: list[str]
    entries: list[FetchManifestEntryOut]


class FetchUploadSessionResponse(RiverhogModel):
    entry: str
    protocol: str
    upload_url: str
    offset: int
    length: int
    checksum_algorithm: str
    expires_at: str | None


class CompleteFetchResponse(RiverhogModel):
    id: str
    state: str
    hot: HotStatusOut

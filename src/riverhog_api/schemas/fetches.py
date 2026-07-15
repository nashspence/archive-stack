from __future__ import annotations

from typing import Literal

from pydantic import Field

from riverhog_api.schemas.archive_restores import ArchiveRestoreListOut
from riverhog_api.schemas.common import RiverhogModel


class FetchSummaryOut(RiverhogModel):
    id: str
    name: str
    collections: list[str]
    state: Literal["draft", "queued_archive", "restoring_archive", "done", "failed"]
    files: int
    bytes: int
    hot_files: int
    hot_bytes: int
    missing_files: int
    missing_bytes: int


class FetchesResponse(RiverhogModel):
    page: int
    per_page: int
    total: int
    pages: int
    fetches: list[FetchSummaryOut]


class CreateFetchRequest(RiverhogModel):
    name: str
    collections: list[str] = Field(default_factory=list)


class FetchCollectionsRequest(RiverhogModel):
    collections: list[str]


class HotEvictRequest(RiverhogModel):
    collections: list[str]
    dry_run: bool = False


class HotEvictResponse(RiverhogModel):
    collections: list[str]
    dry_run: bool
    status: str
    files: int
    bytes: int
    evicted_files: int
    evicted_bytes: int
    would_evict_files: int
    would_evict_bytes: int


class FetchCollectionSummaryOut(RiverhogModel):
    collection: str
    files: int
    bytes: int
    hot_files: int
    hot_bytes: int
    missing_files: int
    missing_bytes: int


class FetchFileOut(RiverhogModel):
    logical_path: str
    collection_id: str
    collection_path: str
    bytes: int
    sha256: str
    hot: bool


class FetchNextActionOut(RiverhogModel):
    action: str
    reason: str


class FetchStatusResponse(FetchSummaryOut):
    collection_summaries: list[FetchCollectionSummaryOut]
    files_preview: list[FetchFileOut]
    next_action: FetchNextActionOut
    archive_restores: ArchiveRestoreListOut


class FetchFilesResponse(RiverhogModel):
    fetch_id: str
    q: str | None
    hot: bool | None
    page: int
    per_page: int
    total: int
    pages: int
    sort: Literal["logical_path", "collection_id", "collection_path", "bytes", "hot"]
    order: Literal["asc", "desc"]
    files: list[FetchFileOut]

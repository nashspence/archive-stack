from __future__ import annotations

from typing import Literal

from riverhog_api.schemas.archive import ArchiveCopyOut
from riverhog_api.schemas.common import RiverhogModel


class ArchiveRestoreNotificationStatusOut(RiverhogModel):
    webhook_configured: bool
    failure_count: int = 0
    last_failure_at: str | None = None
    last_failure: str | None = None


class ArchiveRestoreProgressOut(RiverhogModel):
    archive_verification: Literal["pending", "in_progress", "completed", "failed"]
    extraction: Literal["pending", "in_progress", "completed", "failed"]
    materialization: Literal["pending", "in_progress", "completed", "failed"]


class ArchiveRestoreCollectionOut(RiverhogModel):
    id: str
    archive_copy: ArchiveCopyOut
    stored_bytes: int


class ArchiveRestoreOut(RiverhogModel):
    id: str
    state: Literal["requested", "ready", "expired", "completed", "failed", "canceled"]
    created_at: str
    requested_at: str | None
    ready_at: str | None
    expires_at: str | None
    completed_at: str | None
    canceled_at: str | None = None
    latest_message: str | None
    warnings: list[str]
    notification: ArchiveRestoreNotificationStatusOut
    progress: ArchiveRestoreProgressOut
    collections: list[ArchiveRestoreCollectionOut]


class ArchiveRestoreListOut(RiverhogModel):
    page: int
    per_page: int
    total: int
    pages: int
    sort: Literal["created_at", "id", "state", "ready_at", "expires_at"]
    order: Literal["asc", "desc"]
    terminal: Literal["active", "terminal", "all"] = "all"
    state: Literal["requested", "ready", "expired", "completed", "failed", "canceled"] | None
    collection: str | None
    restores: list[ArchiveRestoreOut]

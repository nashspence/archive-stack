from __future__ import annotations

from enum import StrEnum


class FetchState(StrEnum):
    DRAFT = "draft"
    QUEUED_ARCHIVE = "queued_archive"
    RESTORING_ARCHIVE = "restoring_archive"
    DONE = "done"
    FAILED = "failed"


class ArchiveState(StrEnum):
    PENDING = "pending"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    RETRYING = "retrying"
    FAILED = "failed"


class ArchiveRestoreState(StrEnum):
    REQUESTED = "requested"
    READY = "ready"
    EXPIRED = "expired"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class SearchKind(StrEnum):
    COLLECTION = "collection"
    FILE = "file"

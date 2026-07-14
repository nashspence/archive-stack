from __future__ import annotations

from enum import StrEnum


class FetchState(StrEnum):
    DRAFT = "draft"
    QUEUED_DJDAN = "queued_djdan"
    UPLOADING = "uploading"
    VERIFYING = "verifying"
    QUEUED_ARCHIVE = "queued_archive"
    RESTORING_ARCHIVE = "restoring_archive"
    DONE = "done"
    FAILED = "failed"


class CoverageState(StrEnum):
    NONE = "none"
    PARTIAL = "partial"
    FULL = "full"


class ArchiveState(StrEnum):
    PENDING = "pending"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    RETRYING = "retrying"
    FAILED = "failed"


class ArchiveRestoreState(StrEnum):
    REQUESTED = "requested"
    READY = "ready"
    PAUSED = "paused"
    EXPIRED = "expired"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class DiscState(StrEnum):
    NEEDED = "needed"
    BURNING = "burning"
    VERIFIED = "verified"
    REGISTERED = "registered"
    LOST = "lost"
    DAMAGED = "damaged"
    RETIRED = "retired"


class VerificationState(StrEnum):
    PENDING = "pending"
    VERIFIED = "verified"
    FAILED = "failed"


class SearchKind(StrEnum):
    COLLECTION = "collection"
    FILE = "file"

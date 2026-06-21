from __future__ import annotations

from enum import StrEnum


class FetchState(StrEnum):
    DRAFT = "draft"
    QUEUED_DJDAN = "queued_djdan"
    UPLOADING = "uploading"
    VERIFYING = "verifying"
    QUEUED_CLOUD = "queued_cloud"
    CLOUD_FETCHING = "cloud_fetching"
    DONE = "done"
    FAILED = "failed"


class ProtectionState(StrEnum):
    UNPROTECTED = "unprotected"
    PARTIALLY_PROTECTED = "partially_protected"
    PROTECTED = "protected"


class RecoveryCoverageState(StrEnum):
    NONE = "none"
    PARTIAL = "partial"
    FULL = "full"


class GlacierState(StrEnum):
    PENDING = "pending"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    RETRYING = "retrying"
    FAILED = "failed"


class RecoverySessionState(StrEnum):
    RESTORE_REQUESTED = "restore_requested"
    READY = "ready"
    PAUSED = "paused"
    EXPIRED = "expired"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class CopyState(StrEnum):
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

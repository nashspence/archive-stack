from __future__ import annotations

from enum import StrEnum


class FetchState(StrEnum):
    WAITING_MEDIA = "waiting_media"
    UPLOADING = "uploading"
    VERIFYING = "verifying"
    DONE = "done"
    FAILED = "failed"


class ProtectionState(StrEnum):
    UNPROTECTED = "unprotected"
    PARTIALLY_PROTECTED = "partially_protected"
    PROTECTED = "protected"


class GlacierState(StrEnum):
    PENDING = "pending"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    RETRYING = "retrying"
    FAILED = "failed"


class RecoverySessionState(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    RESTORE_REQUESTED = "restore_requested"
    READY = "ready"
    EXPIRED = "expired"
    COMPLETED = "completed"


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

from __future__ import annotations

from enum import StrEnum


class ArchiveState(StrEnum):
    PENDING = "pending"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    RETRYING = "retrying"
    FAILED = "failed"


class SearchKind(StrEnum):
    COLLECTION = "collection"
    FILE = "file"

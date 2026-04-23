from __future__ import annotations

from enum import StrEnum


class FetchState(StrEnum):
    WAITING_MEDIA = "waiting_media"
    UPLOADING = "uploading"
    VERIFYING = "verifying"
    DONE = "done"
    FAILED = "failed"


class SearchKind(StrEnum):
    COLLECTION = "collection"
    FILE = "file"

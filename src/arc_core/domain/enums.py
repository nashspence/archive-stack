from __future__ import annotations

from enum import Enum


class FetchState(str, Enum):
    WAITING_MEDIA = "waiting_media"
    UPLOADING = "uploading"
    VERIFYING = "verifying"
    DONE = "done"
    FAILED = "failed"


class SearchKind(str, Enum):
    COLLECTION = "collection"
    FILE = "file"

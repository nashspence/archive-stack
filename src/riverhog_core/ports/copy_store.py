from __future__ import annotations

from typing import Protocol

from riverhog_core.domain.models import CopySummary, FetchCopyHint
from riverhog_core.domain.types import CopyId, ImageId


class CopyStore(Protocol):
    def create_copy(self, image_id: ImageId, copy_id: CopyId, location: str) -> CopySummary: ...
    def file_copies(self, collection_id: str, path: str) -> list[FetchCopyHint]: ...

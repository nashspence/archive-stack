from __future__ import annotations

from typing import Protocol

from riverhog_core.domain.models import DiscSummary, FetchDiscHint
from riverhog_core.domain.types import DiscId, ImageId


class DiscStore(Protocol):
    def create_disc(self, image_id: ImageId, disc_id: DiscId, location: str) -> DiscSummary: ...
    def file_discs(self, collection_id: str, path: str) -> list[FetchDiscHint]: ...

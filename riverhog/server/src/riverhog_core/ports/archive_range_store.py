from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol


class ArchiveObjectRangeStore(Protocol):
    """Read one exact byte interval from an immutable archive object."""

    def iter_object_range(
        self,
        *,
        object_path: str,
        version_id: str | None,
        offset: int,
        size: int,
    ) -> Iterator[bytes]: ...

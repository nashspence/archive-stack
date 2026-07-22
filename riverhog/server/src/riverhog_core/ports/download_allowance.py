from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from riverhog_core.domain.models import ArchiveDownloadAllowance


class DownloadAllowance(Protocol):
    def track(
        self,
        *,
        store: str,
        expected_bytes: int,
        content: Iterator[bytes],
    ) -> Iterator[bytes]: ...

    def get_statuses(self) -> tuple[ArchiveDownloadAllowance, ...]: ...

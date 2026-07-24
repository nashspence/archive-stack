from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

from riverhog_core.domain.models import ArchiveDownloadAllowance


@dataclass(frozen=True, slots=True)
class DownloadAttribution:
    key_id: str
    job_id: str


class DownloadAllowance(Protocol):
    def track(
        self,
        *,
        store: str,
        expected_bytes: int,
        content: Iterator[bytes],
        attribution: DownloadAttribution | None = None,
    ) -> Iterator[bytes]: ...

    def reserve_retrieval(
        self,
        *,
        key_id: str,
        job_id: str,
        expected_bytes: int,
        expires_at: str,
    ) -> None: ...

    def release_retrieval(self, *, job_id: str) -> None: ...

    def set_key_quota(
        self,
        *,
        app: str,
        key_id: str,
        monthly_bytes: int | None,
    ) -> dict[str, object]: ...

    def get_key_quota(self, *, key_id: str) -> dict[str, object]: ...

    def list_key_quotas(
        self,
        *,
        page: int,
        per_page: int,
        q: str | None,
        sort: str,
        order: str,
        app: str | None = None,
        active: bool | None = None,
        all_items: bool = False,
    ) -> dict[str, object]: ...

    def get_statuses(self) -> tuple[ArchiveDownloadAllowance, ...]: ...


__all__ = ["DownloadAllowance", "DownloadAttribution"]

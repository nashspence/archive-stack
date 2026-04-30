from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ArchiveUploadReceipt:
    object_path: str
    stored_bytes: int
    backend: str
    storage_class: str
    uploaded_at: str
    verified_at: str | None = None


@dataclass(frozen=True)
class ArchiveRestoreStatus:
    state: str
    ready_at: str | None = None
    expires_at: str | None = None
    message: str | None = None


class ArchiveStore(Protocol):
    def upload_finalized_image(
        self,
        *,
        image_id: str,
        filename: str,
        image_root: Path,
    ) -> ArchiveUploadReceipt: ...

    def request_finalized_image_restore(
        self,
        *,
        image_id: str,
        object_path: str,
        retrieval_tier: str,
        hold_days: int,
        requested_at: str,
        estimated_ready_at: str,
    ) -> ArchiveRestoreStatus: ...

    def get_finalized_image_restore_status(
        self,
        *,
        image_id: str,
        object_path: str,
        requested_at: str,
        estimated_ready_at: str | None,
        estimated_expires_at: str | None,
    ) -> ArchiveRestoreStatus: ...

    def iter_restored_finalized_image(
        self,
        *,
        image_id: str,
        object_path: str,
    ) -> Iterator[bytes]: ...

    def cleanup_finalized_image_restore(
        self,
        *,
        image_id: str,
        object_path: str,
    ) -> None: ...

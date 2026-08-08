from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ImmutableObjectReceipt:
    object_path: str
    version_id: str | None
    etag: str | None
    stored_bytes: int
    stored_sha256: str
    completed_at: str


class ImmutableArchiveObjectStore(Protocol):
    def put_immutable_object(
        self,
        *,
        object_path: str,
        content: bytes,
        content_type: str,
        identity_metadata: dict[str, str],
    ) -> ImmutableObjectReceipt: ...

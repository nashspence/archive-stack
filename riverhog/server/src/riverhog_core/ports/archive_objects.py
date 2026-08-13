from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

from riverhog_core.domain.retrieval_cache import RetrievalCacheReceipt


class ArchiveObjectIdentityConflict(RuntimeError):
    """The requested immutable object key already names a different object."""


@dataclass(frozen=True, slots=True)
class MultipartPartReceipt:
    number: int
    etag: str
    bytes: int
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class MultipartUpload:
    object_path: str
    upload_id: str


@dataclass(frozen=True, slots=True)
class CompletedObjectReceipt:
    object_path: str
    version_id: str | None
    etag: str | None
    bytes: int
    completed_at: str
    retrieval_cache: RetrievalCacheReceipt | None = None


class ArchiveMultipartObjectStore(Protocol):
    def create_multipart_upload(
        self,
        *,
        object_path: str,
        content_type: str,
        metadata: dict[str, str],
    ) -> MultipartUpload: ...

    def upload_part(
        self,
        *,
        upload: MultipartUpload,
        number: int,
        content: bytes,
    ) -> MultipartPartReceipt: ...

    def list_parts(self, *, upload: MultipartUpload) -> tuple[MultipartPartReceipt, ...]: ...

    def complete_multipart_upload(
        self,
        *,
        upload: MultipartUpload,
        parts: tuple[MultipartPartReceipt, ...],
        expected_bytes: int,
        expected_metadata: dict[str, str],
    ) -> CompletedObjectReceipt: ...

    def head_completed_object(
        self,
        *,
        object_path: str,
        expected_metadata: dict[str, str],
    ) -> CompletedObjectReceipt | None: ...

    def abort_multipart_upload(self, *, upload: MultipartUpload) -> None: ...


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


class ArchiveObjectRangeStore(Protocol):
    """Read exact byte intervals from immutable archive objects."""

    def iter_object_range(
        self,
        *,
        object_path: str,
        version_id: str | None,
        offset: int,
        size: int,
    ) -> Iterator[bytes]: ...

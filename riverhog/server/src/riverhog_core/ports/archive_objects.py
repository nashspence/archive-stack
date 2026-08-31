from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

from riverhog_storage_adapter_protocol import ObjectPlacement

from riverhog_core.domain.retrieval_cache import RetrievalCacheReceipt


class ArchiveObjectIdentityConflict(RuntimeError):
    """The requested immutable object key already names a different object."""


@dataclass(frozen=True, slots=True)
class ResumableWriteConstraints:
    minimum_nonfinal_segment_bytes: int
    maximum_segment_bytes: int | None
    maximum_segment_count: int | None


@dataclass(frozen=True, slots=True)
class WriteSegmentReceipt:
    number: int
    segment_token: str
    bytes: int
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class WriteSession:
    object_path: str
    write_token: str
    expected_bytes: int


@dataclass(frozen=True, slots=True)
class CompletedObjectReceipt:
    object_path: str
    revision: str | None
    entity_token: str | None
    bytes: int
    completed_at: str
    retrieval_cache: RetrievalCacheReceipt | None = None


class ArchiveResumableObjectStore(Protocol):
    def write_constraints(self) -> ResumableWriteConstraints: ...

    def begin_write(
        self,
        *,
        object_path: str,
        expected_bytes: int,
        content_type: str,
        metadata: dict[str, str],
    ) -> WriteSession: ...

    def write_segment(
        self,
        *,
        session: WriteSession,
        number: int,
        content: bytes,
    ) -> WriteSegmentReceipt: ...

    def list_segments(self, *, session: WriteSession) -> tuple[WriteSegmentReceipt, ...]: ...

    def complete_write(
        self,
        *,
        session: WriteSession,
        segments: tuple[WriteSegmentReceipt, ...],
        expected_bytes: int,
        expected_content_type: str,
        expected_metadata: dict[str, str],
    ) -> CompletedObjectReceipt: ...

    def find_completed_write(
        self,
        *,
        object_path: str,
        expected_bytes: int,
        expected_content_type: str,
        expected_metadata: dict[str, str],
    ) -> CompletedObjectReceipt | None: ...

    def abort_write(self, *, session: WriteSession) -> None: ...


@dataclass(frozen=True, slots=True)
class ImmutableObjectReceipt:
    object_path: str
    revision: str | None
    entity_token: str | None
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
        required_identity_assertions: dict[str, str],
        placement: ObjectPlacement,
    ) -> ImmutableObjectReceipt: ...


class ArchiveObjectRangeStore(Protocol):
    """Read exact byte intervals from immutable archive objects."""

    def iter_object_range(
        self,
        *,
        object_path: str,
        revision: str | None,
        expected_bytes: int,
        offset: int,
        size: int,
    ) -> Iterator[bytes]: ...

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class ArchiveObjectIdentityConflict(RuntimeError):
    """The requested immutable object key already names a different object."""


@dataclass(frozen=True, slots=True)
class MultipartPartReceipt:
    number: int
    etag: str
    bytes: int


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


class PackUploadCheckpointStore(Protocol):
    def load_pack_upload_checkpoint(self, *, collection_id: int, volume_id: str) -> str | None: ...

    def merge_pack_upload_checkpoint(
        self, *, collection_id: int, volume_id: str, checkpoint_json: str
    ) -> str: ...

    def delete_pack_upload_checkpoint(self, *, collection_id: int, volume_id: str) -> None: ...


class RawUploadCheckpointStore(Protocol):
    def load_raw_upload_checkpoint(self, *, collection_id: int, volume_id: str) -> str | None: ...

    def merge_raw_upload_checkpoint(
        self, *, collection_id: int, volume_id: str, checkpoint_json: str
    ) -> str: ...

    def delete_raw_upload_checkpoint(self, *, collection_id: int, volume_id: str) -> None: ...

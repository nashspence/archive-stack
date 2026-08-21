"""Small provider-driver seam owned by the public adapter support runtime."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

from riverhog_storage_adapter_protocol import (
    CompleteUploadRequest,
    ObjectLocator,
    ObjectReceipt,
    ReadRequest,
    ReadStatus,
    StorageAdapterDescriptor,
    StorageAdapterErrorCode,
    UploadDeclaration,
    UploadPartReceipt,
)


class StorageDriverError(RuntimeError):
    """A provider failure normalized before it reaches Riverhog."""

    def __init__(self, code: StorageAdapterErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ProviderUpload:
    """Private provider upload binding retained only by the adapter journal."""

    upload_id: str

    def __post_init__(self) -> None:
        if not self.upload_id:
            raise ValueError("provider upload ID must not be empty")


class StorageAdapterDriver(Protocol):
    """Provider-specific effects required by the shared v1 runtime."""

    def descriptor(self) -> StorageAdapterDescriptor: ...
    def ready(self) -> None: ...

    def create_upload(self, declaration: UploadDeclaration) -> ProviderUpload: ...

    def upload_part(
        self,
        *,
        declaration: UploadDeclaration,
        upload: ProviderUpload,
        number: int,
        content: bytes,
        stored_sha256: str,
    ) -> str: ...

    def complete_upload(
        self,
        *,
        declaration: UploadDeclaration,
        upload: ProviderUpload,
        completion: CompleteUploadRequest,
    ) -> ObjectReceipt: ...

    def abort_upload(
        self,
        *,
        declaration: UploadDeclaration,
        upload: ProviderUpload,
    ) -> None: ...

    def verify_object(self, receipt: ObjectReceipt) -> None: ...

    def iter_object_content(
        self,
        locator: ObjectLocator,
        *,
        offset: int | None,
        size: int | None,
    ) -> Iterator[bytes]: ...

    def delete_object(self, locator: ObjectLocator) -> None: ...
    def delete_prefix(self, object_prefix: str) -> int: ...
    def prepare_read(self, request: ReadRequest) -> ReadStatus: ...
    def read_status(self, request: ReadRequest) -> ReadStatus: ...
    def cleanup_read(self, request: ReadRequest) -> None: ...
    def abort_incomplete_uploads(self, *, initiated_before: str) -> int: ...

    def verify_part_receipt(
        self,
        *,
        declaration: UploadDeclaration,
        upload: ProviderUpload,
        receipt: UploadPartReceipt,
    ) -> None: ...


__all__ = [
    "ProviderUpload",
    "StorageAdapterDriver",
    "StorageDriverError",
]

from __future__ import annotations

import hashlib
from collections.abc import Iterator

from riverhog_storage_adapter_protocol import (
    MultipartCompleteRequest as AdapterMultipartCompleteRequest,
)
from riverhog_storage_adapter_protocol import (
    MultipartCreateRequest as AdapterMultipartCreateRequest,
)
from riverhog_storage_adapter_protocol import MultipartHeadRequest as AdapterMultipartHeadRequest
from riverhog_storage_adapter_protocol import MultipartPartReceipt as AdapterMultipartPartReceipt
from riverhog_storage_adapter_protocol import MultipartUpload as AdapterMultipartUpload
from riverhog_storage_adapter_protocol import (
    ObjectLocator,
    ObjectPlacement,
    ObjectReadRequest,
    SmallObjectWriteRequest,
    StorageAdapterPort,
    StorageAdapterRejection,
)

from riverhog_core.ports.archive_objects import (
    ArchiveObjectIdentityConflict,
    CompletedObjectReceipt,
    ImmutableObjectReceipt,
    MultipartPartReceipt,
    MultipartUpload,
)


class StorageAdapterArchiveMultipartObjectStore:
    """Riverhog's existing multipart port over one opaque-object adapter."""

    def __init__(
        self,
        adapter: StorageAdapterPort,
        *,
        placement: ObjectPlacement = "archive",
    ) -> None:
        self._adapter = adapter
        self._placement = placement

    def create_multipart_upload(
        self,
        *,
        object_path: str,
        content_type: str,
        metadata: dict[str, str],
    ) -> MultipartUpload:
        upload = self._adapter.create_multipart_upload(
            AdapterMultipartCreateRequest(
                object_path=object_path,
                content_type=content_type,
                identity_metadata=metadata,
                placement=self._placement,
            )
        )
        return _multipart_upload(upload)

    def upload_part(
        self,
        *,
        upload: MultipartUpload,
        number: int,
        content: bytes,
    ) -> MultipartPartReceipt:
        receipt = self._adapter.upload_part(
            upload=_adapter_upload(upload),
            number=number,
            content=content,
        )
        return _multipart_part(receipt)

    def list_parts(self, *, upload: MultipartUpload) -> tuple[MultipartPartReceipt, ...]:
        return tuple(
            _multipart_part(current)
            for current in self._adapter.list_parts(_adapter_upload(upload))
        )

    def complete_multipart_upload(
        self,
        *,
        upload: MultipartUpload,
        parts: tuple[MultipartPartReceipt, ...],
        expected_bytes: int,
        expected_metadata: dict[str, str],
    ) -> CompletedObjectReceipt:
        try:
            receipt = self._adapter.complete_multipart_upload(
                AdapterMultipartCompleteRequest(
                    upload=_adapter_upload(upload),
                    parts=tuple(_adapter_part(current) for current in parts),
                    expected_bytes=expected_bytes,
                    expected_identity_metadata=expected_metadata,
                    expected_placement=self._placement,
                )
            )
        except StorageAdapterRejection as exc:
            _raise_identity_conflict(exc)
            raise
        return CompletedObjectReceipt(
            object_path=receipt.object_path,
            version_id=receipt.revision,
            etag=receipt.entity_token,
            bytes=receipt.stored_bytes,
            completed_at=receipt.completed_at,
        )

    def head_completed_object(
        self,
        *,
        object_path: str,
        expected_metadata: dict[str, str],
    ) -> CompletedObjectReceipt | None:
        try:
            receipt = self._adapter.head_completed_object(
                AdapterMultipartHeadRequest(
                    object_path=object_path,
                    expected_identity_metadata=expected_metadata,
                    expected_placement=self._placement,
                )
            )
        except StorageAdapterRejection as exc:
            _raise_identity_conflict(exc)
            raise
        if receipt is None:
            return None
        return CompletedObjectReceipt(
            object_path=receipt.object_path,
            version_id=receipt.revision,
            etag=receipt.entity_token,
            bytes=receipt.stored_bytes,
            completed_at=receipt.completed_at,
        )

    def abort_multipart_upload(self, *, upload: MultipartUpload) -> None:
        self._adapter.abort_multipart_upload(_adapter_upload(upload))


class StorageAdapterImmutableArchiveObjectStore:
    """Riverhog's create-only small-object port over one opaque-object adapter."""

    def __init__(self, adapter: StorageAdapterPort) -> None:
        self._adapter = adapter

    def put_immutable_object(
        self,
        *,
        object_path: str,
        content: bytes,
        content_type: str,
        identity_metadata: dict[str, str],
        placement: ObjectPlacement,
    ) -> ImmutableObjectReceipt:
        if not object_path or not content or not content_type:
            raise ValueError("immutable archive object identity and content are required")
        try:
            receipt = self._adapter.put_small_object(
                SmallObjectWriteRequest(
                    object_path=object_path,
                    content_type=content_type,
                    identity_metadata=identity_metadata,
                    placement=placement,
                    mode="create_only",
                    stored_bytes=len(content),
                    stored_sha256=hashlib.sha256(content).hexdigest(),
                ),
                content,
            )
        except StorageAdapterRejection as exc:
            _raise_identity_conflict(exc)
            raise
        return ImmutableObjectReceipt(
            object_path=receipt.object_path,
            version_id=receipt.revision,
            etag=receipt.entity_token,
            stored_bytes=receipt.stored_bytes,
            stored_sha256=receipt.stored_sha256,
            completed_at=receipt.completed_at,
        )


class StorageAdapterArchiveObjectRangeStore:
    """Exact ranged reads through the adapter's validated streaming response."""

    def __init__(self, adapter: StorageAdapterPort) -> None:
        self._adapter = adapter

    def iter_object_range(
        self,
        *,
        object_path: str,
        version_id: str | None,
        expected_bytes: int,
        offset: int,
        size: int,
    ) -> Iterator[bytes]:
        return self._adapter.iter_object(
            ObjectReadRequest(
                object=ObjectLocator(object_path=object_path, revision=version_id),
                expected_bytes=expected_bytes,
                offset=offset,
                size=size,
            )
        )


def _adapter_upload(upload: MultipartUpload) -> AdapterMultipartUpload:
    return AdapterMultipartUpload(object_path=upload.object_path, upload_id=upload.upload_id)


def _multipart_upload(upload: AdapterMultipartUpload) -> MultipartUpload:
    return MultipartUpload(object_path=upload.object_path, upload_id=upload.upload_id)


def _adapter_part(part: MultipartPartReceipt) -> AdapterMultipartPartReceipt:
    return AdapterMultipartPartReceipt(
        number=part.number,
        part_token=part.etag,
        stored_bytes=part.bytes,
        stored_sha256=part.sha256,
    )


def _multipart_part(part: AdapterMultipartPartReceipt) -> MultipartPartReceipt:
    return MultipartPartReceipt(
        number=part.number,
        etag=part.part_token,
        bytes=part.stored_bytes,
        sha256=part.stored_sha256,
    )


def _raise_identity_conflict(exc: StorageAdapterRejection) -> None:
    if exc.code == "identity_conflict":
        raise ArchiveObjectIdentityConflict(str(exc)) from exc


__all__ = [
    "StorageAdapterArchiveMultipartObjectStore",
    "StorageAdapterArchiveObjectRangeStore",
    "StorageAdapterImmutableArchiveObjectStore",
]

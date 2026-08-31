from __future__ import annotations

import hashlib
from collections.abc import Iterator

from riverhog_storage_adapter_protocol import (
    CompletedWriteLookupRequest,
    ObjectLocator,
    ObjectPlacement,
    ObjectReadRequest,
    SmallObjectWriteRequest,
    StorageAdapterPort,
    StorageAdapterRejection,
    WriteCompleteRequest,
    WriteStartRequest,
    validated_storage_adapter,
)
from riverhog_storage_adapter_protocol import (
    WriteSegmentReceipt as AdapterWriteSegmentReceipt,
)
from riverhog_storage_adapter_protocol import (
    WriteSession as AdapterWriteSession,
)

from riverhog_core.ports.archive_objects import (
    ArchiveObjectIdentityConflict,
    CompletedObjectReceipt,
    ImmutableObjectReceipt,
    ResumableWriteConstraints,
    WriteSegmentReceipt,
    WriteSession,
)


class StorageAdapterArchiveResumableObjectStore:
    """Riverhog's resumable-write port over one opaque-object adapter."""

    def __init__(
        self,
        adapter: StorageAdapterPort,
        *,
        placement: ObjectPlacement = "archive",
    ) -> None:
        self._adapter = validated_storage_adapter(adapter)
        self._placement = placement

    def write_constraints(self) -> ResumableWriteConstraints:
        descriptor = self._adapter.descriptor()
        return ResumableWriteConstraints(
            minimum_nonfinal_segment_bytes=descriptor.minimum_nonfinal_segment_bytes,
            maximum_segment_bytes=descriptor.maximum_segment_bytes,
            maximum_segment_count=descriptor.maximum_segment_count,
        )

    def begin_write(
        self,
        *,
        object_path: str,
        expected_bytes: int,
        content_type: str,
        metadata: dict[str, str],
    ) -> WriteSession:
        session = self._adapter.begin_write(
            WriteStartRequest(
                object_path=object_path,
                expected_bytes=expected_bytes,
                content_type=content_type,
                required_identity_assertions=metadata,
                placement=self._placement,
            )
        )
        return _write_session(session)

    def write_segment(
        self,
        *,
        session: WriteSession,
        number: int,
        content: bytes,
    ) -> WriteSegmentReceipt:
        receipt = self._adapter.write_segment(
            session=_adapter_session(session),
            number=number,
            stored_bytes=len(content),
            content=content,
        )
        return _write_segment(receipt)

    def list_segments(self, *, session: WriteSession) -> tuple[WriteSegmentReceipt, ...]:
        return tuple(
            _write_segment(current)
            for current in self._adapter.list_segments(_adapter_session(session)).segments
        )

    def complete_write(
        self,
        *,
        session: WriteSession,
        segments: tuple[WriteSegmentReceipt, ...],
        expected_bytes: int,
        expected_content_type: str,
        expected_metadata: dict[str, str],
    ) -> CompletedObjectReceipt:
        request = WriteCompleteRequest(
            session=_adapter_session(session),
            segments=tuple(_adapter_segment(current) for current in segments),
            expected_bytes=expected_bytes,
            expected_content_type=expected_content_type,
            required_identity_assertions=expected_metadata,
            expected_placement=self._placement,
        )
        try:
            receipt = self._adapter.complete_write(request)
        except StorageAdapterRejection as exc:
            _raise_identity_conflict(exc)
            raise
        return CompletedObjectReceipt(
            object_path=receipt.object_path,
            revision=receipt.revision,
            entity_token=receipt.entity_token,
            bytes=receipt.stored_bytes,
            completed_at=receipt.completed_at,
        )

    def find_completed_write(
        self,
        *,
        object_path: str,
        expected_bytes: int,
        expected_content_type: str,
        expected_metadata: dict[str, str],
    ) -> CompletedObjectReceipt | None:
        request = CompletedWriteLookupRequest(
            object_path=object_path,
            expected_bytes=expected_bytes,
            expected_content_type=expected_content_type,
            required_identity_assertions=expected_metadata,
            expected_placement=self._placement,
        )
        try:
            receipt = self._adapter.find_completed_write(request)
        except StorageAdapterRejection as exc:
            _raise_identity_conflict(exc)
            raise
        if receipt is None:
            return None
        return CompletedObjectReceipt(
            object_path=receipt.object_path,
            revision=receipt.revision,
            entity_token=receipt.entity_token,
            bytes=receipt.stored_bytes,
            completed_at=receipt.completed_at,
        )

    def abort_write(self, *, session: WriteSession) -> None:
        self._adapter.abort_write(_adapter_session(session))


class StorageAdapterImmutableArchiveObjectStore:
    """Riverhog's create-only small-object port over one opaque-object adapter."""

    def __init__(self, adapter: StorageAdapterPort) -> None:
        self._adapter = validated_storage_adapter(adapter)

    def put_immutable_object(
        self,
        *,
        object_path: str,
        content: bytes,
        content_type: str,
        required_identity_assertions: dict[str, str],
        placement: ObjectPlacement,
    ) -> ImmutableObjectReceipt:
        if not object_path or not content or not content_type:
            raise ValueError("immutable archive object identity and content are required")
        try:
            receipt = self._adapter.put_small_object(
                SmallObjectWriteRequest(
                    object_path=object_path,
                    content_type=content_type,
                    required_identity_assertions=required_identity_assertions,
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
            revision=receipt.revision,
            entity_token=receipt.entity_token,
            stored_bytes=receipt.stored_bytes,
            stored_sha256=receipt.stored_sha256,
            completed_at=receipt.completed_at,
        )


class StorageAdapterArchiveObjectRangeStore:
    """Exact ranged reads through the adapter's validated streaming response."""

    def __init__(self, adapter: StorageAdapterPort) -> None:
        self._adapter = validated_storage_adapter(adapter)

    def iter_object_range(
        self,
        *,
        object_path: str,
        revision: str | None,
        expected_bytes: int,
        offset: int,
        size: int,
    ) -> Iterator[bytes]:
        return self._adapter.read_object(
            ObjectReadRequest(
                object=ObjectLocator(object_path=object_path, revision=revision),
                expected_bytes=expected_bytes,
                offset=offset,
                size=size,
            )
        ).content


def _adapter_session(session: WriteSession) -> AdapterWriteSession:
    return AdapterWriteSession(
        object_path=session.object_path,
        expected_bytes=session.expected_bytes,
        write_token=session.write_token,
    )


def _write_session(session: AdapterWriteSession) -> WriteSession:
    return WriteSession(
        object_path=session.object_path,
        write_token=session.write_token,
        expected_bytes=session.expected_bytes,
    )


def _adapter_segment(segment: WriteSegmentReceipt) -> AdapterWriteSegmentReceipt:
    return AdapterWriteSegmentReceipt(
        number=segment.number,
        segment_token=segment.segment_token,
        stored_bytes=segment.bytes,
        stored_sha256=segment.sha256,
    )


def _write_segment(segment: AdapterWriteSegmentReceipt) -> WriteSegmentReceipt:
    return WriteSegmentReceipt(
        number=segment.number,
        segment_token=segment.segment_token,
        bytes=segment.stored_bytes,
        sha256=segment.stored_sha256,
    )


def _raise_identity_conflict(exc: StorageAdapterRejection) -> None:
    if exc.code == "identity_conflict":
        raise ArchiveObjectIdentityConflict(str(exc)) from exc


__all__ = [
    "StorageAdapterArchiveResumableObjectStore",
    "StorageAdapterArchiveObjectRangeStore",
    "StorageAdapterImmutableArchiveObjectStore",
]

from __future__ import annotations

import hashlib

import pytest
from riverhog_core.ports.archive_objects import ArchiveObjectIdentityConflict
from riverhog_core.stores.storage_adapter_archive_objects import (
    StorageAdapterArchiveObjectRangeStore,
    StorageAdapterArchiveResumableObjectStore,
    StorageAdapterImmutableArchiveObjectStore,
)
from riverhog_storage_adapter_protocol import (
    AdapterDescriptor,
    CompletedObjectReceipt,
    CompletedWriteLookupRequest,
    ImmutableObjectReceipt,
    ObjectLocator,
    ObjectReadReceipt,
    ObjectReadRequest,
    ObjectReadStream,
    SmallObjectWriteRequest,
    StorageAdapterRejection,
    WriteCompleteRequest,
    WriteSegmentReceipt,
    WriteSegmentSet,
    WriteSession,
    WriteStartRequest,
)


class _Adapter:
    def __init__(self) -> None:
        self.created: WriteStartRequest | None = None
        self.upload: WriteSession | None = None
        self.parts: dict[int, bytes] = {}
        self.small: SmallObjectWriteRequest | None = None
        self.objects = {
            "archives/id/volume.age": b"firstsecond",
            "archives/id/manifest.age": b"manifest-ciphertext",
        }

    def descriptor(self) -> AdapterDescriptor:
        return AdapterDescriptor(
            implementation_id="fixture.storage/v1",
            implementation_version="1.0.0",
            read_mode="immediate",
            minimum_nonfinal_segment_bytes=1,
        )

    def begin_write(self, request: WriteStartRequest) -> WriteSession:
        self.created = request
        self.upload = WriteSession(
            object_path=request.object_path,
            write_token="upload-1",
            expected_bytes=request.expected_bytes,
        )
        return self.upload

    def write_segment(
        self,
        *,
        session: WriteSession,
        number: int,
        stored_bytes: int,
        content: bytes,
    ) -> WriteSegmentReceipt:
        assert self.upload is not None
        assert session == self.upload
        assert len(content) == stored_bytes
        self.parts[number] = content
        return WriteSegmentReceipt(
            number=number,
            segment_token=f"part-{number}",
            stored_bytes=len(content),
            stored_sha256=hashlib.sha256(content).hexdigest(),
        )

    def list_segments(self, session: WriteSession) -> WriteSegmentSet:
        assert self.upload is not None
        assert session == self.upload
        return WriteSegmentSet(
            session=session,
            segments=tuple(
                WriteSegmentReceipt(
                    number=number,
                    segment_token=f"part-{number}",
                    stored_bytes=len(content),
                    stored_sha256=hashlib.sha256(content).hexdigest(),
                )
                for number, content in sorted(self.parts.items())
            ),
        )

    def complete_write(
        self,
        request: WriteCompleteRequest,
    ) -> CompletedObjectReceipt:
        assert request.expected_placement == "archive"
        assert request.required_identity_assertions == {"riverhog-format": "volume/v1"}
        return self._completed(request)

    def find_completed_write(
        self,
        request: CompletedWriteLookupRequest,
    ) -> CompletedObjectReceipt | None:
        assert request.expected_placement == "archive"
        return self._completed(request)

    def abort_write(self, session: WriteSession) -> None:
        assert self.upload is not None
        assert session == self.upload

    def put_small_object(
        self,
        request: SmallObjectWriteRequest,
        content: bytes,
    ) -> ImmutableObjectReceipt:
        self.small = request
        return ImmutableObjectReceipt(
            object_path=request.object_path,
            revision="version-small",
            entity_token="entity-small",
            stored_bytes=len(content),
            stored_sha256=hashlib.sha256(content).hexdigest(),
            verified_content_type=request.content_type,
            verified_identity_assertions=request.required_identity_assertions,
            verified_placement=request.placement,
            completed_at="2026-08-21T00:00:00.000000Z",
        )

    def read_object(self, request: ObjectReadRequest) -> ObjectReadStream:
        content = self.objects[request.object.object_path]
        assert request.expected_bytes == len(content)
        assert request.offset is not None and request.size is not None
        selected = content[request.offset : request.offset + request.size]
        return ObjectReadStream(
            receipt=ObjectReadReceipt(
                object=ObjectLocator(
                    object_path=request.object.object_path,
                    revision=request.object.revision,
                ),
                total_bytes=len(content),
                offset=request.offset,
                read_bytes=len(selected),
            ),
            content=iter((selected,)) if selected else iter(()),
        )

    @staticmethod
    def _completed(
        request: WriteCompleteRequest | CompletedWriteLookupRequest,
    ) -> CompletedObjectReceipt:
        return CompletedObjectReceipt(
            object_path="archives/id/volume.age",
            revision="version-volume",
            entity_token="entity-volume",
            stored_bytes=11,
            verified_content_type=request.expected_content_type,
            verified_identity_assertions=request.required_identity_assertions,
            verified_placement=request.expected_placement,
            completed_at="2026-08-21T00:00:00.000000Z",
        )


def test_existing_object_ports_preserve_adapter_receipts_and_generic_placement() -> None:
    adapter = _Adapter()
    store = StorageAdapterArchiveResumableObjectStore(adapter)  # type: ignore[arg-type]
    session = store.begin_write(
        object_path="archives/id/volume.age",
        expected_bytes=11,
        content_type="application/octet-stream",
        metadata={"riverhog-format": "volume/v1"},
    )
    segments = (
        store.write_segment(session=session, number=1, content=b"first"),
        store.write_segment(session=session, number=2, content=b"second"),
    )
    completed = store.complete_write(
        session=session,
        segments=segments,
        expected_bytes=11,
        expected_content_type="application/octet-stream",
        expected_metadata={"riverhog-format": "volume/v1"},
    )

    assert adapter.created is not None and adapter.created.placement == "archive"
    assert store.list_segments(session=session) == segments
    assert completed.revision == "version-volume"
    assert completed.entity_token == "entity-volume"
    assert (
        store.find_completed_write(
            object_path=session.object_path,
            expected_bytes=11,
            expected_content_type="application/octet-stream",
            expected_metadata={"riverhog-format": "volume/v1"},
        )
        == completed
    )

    immutable = StorageAdapterImmutableArchiveObjectStore(adapter)  # type: ignore[arg-type]
    content = adapter.objects["archives/id/manifest.age"]
    receipt = immutable.put_immutable_object(
        object_path="archives/id/manifest.age",
        content=content,
        content_type="application/octet-stream",
        required_identity_assertions={"riverhog-format": "manifest/v1"},
        placement="immediate",
    )
    assert adapter.small is not None and adapter.small.placement == "immediate"
    assert receipt.revision == "version-small"
    assert receipt.stored_sha256 == hashlib.sha256(content).hexdigest()


def test_range_bridge_binds_the_exact_total_object_length() -> None:
    adapter = _Adapter()
    store = StorageAdapterArchiveObjectRangeStore(adapter)  # type: ignore[arg-type]

    assert (
        b"".join(
            store.iter_object_range(
                object_path="archives/id/volume.age",
                revision="version-volume",
                expected_bytes=11,
                offset=5,
                size=6,
            )
        )
        == b"second"
    )


def test_identity_conflicts_keep_the_existing_internal_exception() -> None:
    class _ConflictAdapter(_Adapter):
        def find_completed_write(
            self,
            request: CompletedWriteLookupRequest,
        ) -> CompletedObjectReceipt | None:
            _ = request
            raise StorageAdapterRejection("identity_conflict", "different object")

    store = StorageAdapterArchiveResumableObjectStore(
        _ConflictAdapter()  # type: ignore[arg-type]
    )
    with pytest.raises(ArchiveObjectIdentityConflict, match="different object"):
        store.find_completed_write(
            object_path="archives/id/volume.age",
            expected_bytes=11,
            expected_content_type="application/octet-stream",
            expected_metadata={"riverhog-format": "volume/v1"},
        )


def test_completed_receipts_must_attest_the_exact_requested_storage_predicates() -> None:
    class _FalseAttestationAdapter(_Adapter):
        def complete_write(
            self,
            request: WriteCompleteRequest,
        ) -> CompletedObjectReceipt:
            return self._completed(request).model_copy(update={"verified_placement": "immediate"})

    store = StorageAdapterArchiveResumableObjectStore(
        _FalseAttestationAdapter()  # type: ignore[arg-type]
    )
    session = store.begin_write(
        object_path="archives/id/volume.age",
        expected_bytes=11,
        content_type="application/octet-stream",
        metadata={"riverhog-format": "volume/v1"},
    )
    segment = store.write_segment(session=session, number=1, content=b"firstsecond")
    with pytest.raises(ValueError, match="placement"):
        store.complete_write(
            session=session,
            segments=(segment,),
            expected_bytes=11,
            expected_content_type="application/octet-stream",
            expected_metadata={"riverhog-format": "volume/v1"},
        )

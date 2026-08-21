from __future__ import annotations

import hashlib
from collections.abc import Iterator

import pytest
from riverhog_core.ports.archive_objects import ArchiveObjectIdentityConflict
from riverhog_core.stores.storage_adapter_archive_objects import (
    StorageAdapterArchiveMultipartObjectStore,
    StorageAdapterArchiveObjectRangeStore,
    StorageAdapterImmutableArchiveObjectStore,
)
from riverhog_storage_adapter_protocol import (
    CompletedObjectReceipt,
    ImmutableObjectReceipt,
    MultipartCompleteRequest,
    MultipartCreateRequest,
    MultipartHeadRequest,
    MultipartPartReceipt,
    MultipartUpload,
    ObjectReadRequest,
    SmallObjectWriteRequest,
    StorageAdapterRejection,
)


class _Adapter:
    def __init__(self) -> None:
        self.created: MultipartCreateRequest | None = None
        self.upload = MultipartUpload(object_path="archives/id/volume.age", upload_id="upload-1")
        self.parts: dict[int, bytes] = {}
        self.small: SmallObjectWriteRequest | None = None
        self.objects = {
            "archives/id/volume.age": b"firstsecond",
            "archives/id/manifest.age": b"manifest-ciphertext",
        }

    def create_multipart_upload(self, request: MultipartCreateRequest) -> MultipartUpload:
        self.created = request
        return self.upload

    def upload_part(
        self,
        *,
        upload: MultipartUpload,
        number: int,
        content: bytes,
    ) -> MultipartPartReceipt:
        assert upload == self.upload
        self.parts[number] = content
        return MultipartPartReceipt(
            number=number,
            part_token=f"part-{number}",
            stored_bytes=len(content),
            stored_sha256=hashlib.sha256(content).hexdigest(),
        )

    def list_parts(self, upload: MultipartUpload) -> tuple[MultipartPartReceipt, ...]:
        return tuple(
            MultipartPartReceipt(
                number=number,
                part_token=f"part-{number}",
                stored_bytes=len(content),
                stored_sha256=hashlib.sha256(content).hexdigest(),
            )
            for number, content in sorted(self.parts.items())
        )

    def complete_multipart_upload(
        self,
        request: MultipartCompleteRequest,
    ) -> CompletedObjectReceipt:
        assert request.expected_placement == "archive"
        assert request.expected_identity_metadata == {"riverhog-format": "volume/v1"}
        return self._completed()

    def head_completed_object(
        self,
        request: MultipartHeadRequest,
    ) -> CompletedObjectReceipt | None:
        assert request.expected_placement == "archive"
        return self._completed()

    def abort_multipart_upload(self, upload: MultipartUpload) -> None:
        assert upload == self.upload

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
            completed_at="2026-08-21T00:00:00Z",
        )

    def iter_object(self, request: ObjectReadRequest) -> Iterator[bytes]:
        content = self.objects[request.object.object_path]
        assert request.expected_bytes == len(content)
        assert request.offset is not None and request.size is not None
        yield content[request.offset : request.offset + request.size]

    @staticmethod
    def _completed() -> CompletedObjectReceipt:
        return CompletedObjectReceipt(
            object_path="archives/id/volume.age",
            revision="version-volume",
            entity_token="entity-volume",
            stored_bytes=11,
            completed_at="2026-08-21T00:00:00Z",
        )


def test_existing_object_ports_preserve_adapter_receipts_and_generic_placement() -> None:
    adapter = _Adapter()
    multipart = StorageAdapterArchiveMultipartObjectStore(adapter)  # type: ignore[arg-type]
    upload = multipart.create_multipart_upload(
        object_path="archives/id/volume.age",
        content_type="application/octet-stream",
        metadata={"riverhog-format": "volume/v1"},
    )
    parts = (
        multipart.upload_part(upload=upload, number=1, content=b"first"),
        multipart.upload_part(upload=upload, number=2, content=b"second"),
    )
    completed = multipart.complete_multipart_upload(
        upload=upload,
        parts=parts,
        expected_bytes=11,
        expected_metadata={"riverhog-format": "volume/v1"},
    )

    assert adapter.created is not None and adapter.created.placement == "archive"
    assert multipart.list_parts(upload=upload) == parts
    assert completed.version_id == "version-volume"
    assert completed.etag == "entity-volume"
    assert (
        multipart.head_completed_object(
            object_path=upload.object_path,
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
        identity_metadata={"riverhog-format": "manifest/v1"},
        placement="immediate",
    )
    assert adapter.small is not None and adapter.small.placement == "immediate"
    assert receipt.version_id == "version-small"
    assert receipt.stored_sha256 == hashlib.sha256(content).hexdigest()


def test_range_bridge_binds_the_exact_total_object_length() -> None:
    adapter = _Adapter()
    store = StorageAdapterArchiveObjectRangeStore(adapter)  # type: ignore[arg-type]

    assert (
        b"".join(
            store.iter_object_range(
                object_path="archives/id/volume.age",
                version_id="version-volume",
                expected_bytes=11,
                offset=5,
                size=6,
            )
        )
        == b"second"
    )


def test_identity_conflicts_keep_the_existing_internal_exception() -> None:
    class _ConflictAdapter(_Adapter):
        def head_completed_object(
            self,
            request: MultipartHeadRequest,
        ) -> CompletedObjectReceipt | None:
            _ = request
            raise StorageAdapterRejection("identity_conflict", "different object")

    store = StorageAdapterArchiveMultipartObjectStore(
        _ConflictAdapter()  # type: ignore[arg-type]
    )
    with pytest.raises(ArchiveObjectIdentityConflict, match="different object"):
        store.head_completed_object(
            object_path="archives/id/volume.age",
            expected_metadata={"riverhog-format": "volume/v1"},
        )

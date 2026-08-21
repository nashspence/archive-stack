from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Iterator

import pytest
from riverhog_core.ports.archive_objects import CompletedObjectReceipt, MultipartPartReceipt
from riverhog_core.stores.storage_adapter_retrieval_cache import StorageAdapterRetrievalCache
from riverhog_core.throughput import ArchiveThroughputTuning, ArchiveTransferResources
from riverhog_storage_adapter_protocol import (
    AbortIncompleteUploadsRequest,
    AdapterDescriptor,
    DeleteObjectRequest,
    ImmutableObjectReceipt,
    MultipartCompleteRequest,
    MultipartCreateRequest,
    MultipartHeadRequest,
    ObjectReadRequest,
    SmallObjectWriteRequest,
)
from riverhog_storage_adapter_protocol import (
    CompletedObjectReceipt as AdapterCompletedObjectReceipt,
)
from riverhog_storage_adapter_protocol import (
    MultipartPartReceipt as AdapterMultipartPartReceipt,
)
from riverhog_storage_adapter_protocol import (
    MultipartUpload as AdapterMultipartUpload,
)

_PART_BYTES = 5 * 1024 * 1024


class _Adapter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._two_parts_active = threading.Event()
        self._parts: dict[int, bytes] = {}
        self.objects: dict[str, bytes] = {}
        self.revisions: dict[str, str] = {}
        self.active = 0
        self.maximum_active = 0
        self.created: MultipartCreateRequest | None = None
        self.deleted: list[DeleteObjectRequest] = []
        self.reads = 0

    def descriptor(self) -> AdapterDescriptor:
        return AdapterDescriptor(
            implementation_id="fixture.cache/v1",
            implementation_version="1.0.0",
            read_mode="immediate",
            minimum_nonfinal_part_bytes=_PART_BYTES,
            maximum_part_bytes=32 * 1024 * 1024,
            maximum_part_count=10_000,
        )

    def create_multipart_upload(
        self,
        request: MultipartCreateRequest,
    ) -> AdapterMultipartUpload:
        self.created = request
        return AdapterMultipartUpload(object_path=request.object_path, upload_id="upload-1")

    def upload_part(
        self,
        *,
        upload: AdapterMultipartUpload,
        number: int,
        content: bytes,
    ) -> AdapterMultipartPartReceipt:
        assert upload.upload_id == "upload-1"
        with self._lock:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            if self.active >= 2:
                self._two_parts_active.set()
        if number <= 2:
            assert self._two_parts_active.wait(timeout=5)
        self._parts[number] = content
        with self._lock:
            self.active -= 1
        return AdapterMultipartPartReceipt(
            number=number,
            part_token=f"part-{number}",
            stored_bytes=len(content),
            stored_sha256=hashlib.sha256(content).hexdigest(),
        )

    def list_parts(
        self,
        upload: AdapterMultipartUpload,
    ) -> tuple[AdapterMultipartPartReceipt, ...]:
        assert upload.upload_id == "upload-1"
        return tuple(
            AdapterMultipartPartReceipt(
                number=number,
                part_token=f"part-{number}",
                stored_bytes=len(content),
                stored_sha256=hashlib.sha256(content).hexdigest(),
            )
            for number, content in sorted(self._parts.items())
        )

    def complete_multipart_upload(
        self,
        request: MultipartCompleteRequest,
    ) -> AdapterCompletedObjectReceipt:
        content = b"".join(self._parts[current.number] for current in request.parts)
        assert len(content) == request.expected_bytes
        self.objects[request.upload.object_path] = content
        self.revisions[request.upload.object_path] = "version-1"
        return self._completed(request.upload.object_path)

    def head_completed_object(
        self,
        request: MultipartHeadRequest,
    ) -> AdapterCompletedObjectReceipt | None:
        if request.object_path not in self.objects:
            return None
        return self._completed(request.object_path)

    def abort_multipart_upload(self, upload: AdapterMultipartUpload) -> None:
        assert upload.upload_id == "upload-1"

    def put_small_object(
        self,
        request: SmallObjectWriteRequest,
        content: bytes,
    ) -> ImmutableObjectReceipt:
        assert request.placement == "immediate"
        assert len(content) == request.stored_bytes
        assert hashlib.sha256(content).hexdigest() == request.stored_sha256
        self.objects[request.object_path] = content
        self.revisions[request.object_path] = "version-small"
        return ImmutableObjectReceipt(
            object_path=request.object_path,
            revision="version-small",
            entity_token="entity-small",
            stored_bytes=len(content),
            stored_sha256=request.stored_sha256,
            completed_at="2026-08-21T00:00:00Z",
        )

    def iter_object(self, request: ObjectReadRequest) -> Iterator[bytes]:
        self.reads += 1
        content = self.objects[request.object.object_path]
        assert len(content) == request.expected_bytes
        if request.offset is not None and request.size is not None:
            content = content[request.offset : request.offset + request.size]
        yield content

    def delete_object(self, request: DeleteObjectRequest) -> None:
        self.deleted.append(request)
        self.objects.pop(request.object.object_path, None)

    def abort_incomplete_uploads(self, request: AbortIncompleteUploadsRequest) -> int:
        assert request.object_prefix == "objects/"
        return 2

    def _completed(self, object_path: str) -> AdapterCompletedObjectReceipt:
        return AdapterCompletedObjectReceipt(
            object_path=object_path,
            revision=self.revisions[object_path],
            entity_token="entity-1",
            stored_bytes=len(self.objects[object_path]),
            completed_at="2026-08-21T00:00:00Z",
        )


def _cache(adapter: _Adapter) -> StorageAdapterRetrievalCache:
    tuning = ArchiveThroughputTuning(
        multipart_concurrency=2,
        upload_request_concurrency=2,
        upload_max_inflight_bytes=3 * _PART_BYTES,
    )
    return StorageAdapterRetrievalCache(
        adapter,  # type: ignore[arg-type]
        multipart_part_bytes=_PART_BYTES,
        throughput_tuning=tuning,
        transfer_resources=ArchiveTransferResources.from_tuning(tuning),
    )


def test_cache_hydration_preserves_bounded_overlapping_uploads_without_reread(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="riverhog.transfer")
    adapter = _Adapter()
    cache = _cache(adapter)
    content = b"a" * _PART_BYTES + b"b" * _PART_BYTES + b"tail"

    receipt = cache.put(
        source_store="archive",
        collection_id=1,
        object_id="raw-000001",
        content=(
            content[offset : offset + 1024 * 1024] for offset in range(0, len(content), 1024 * 1024)
        ),
        content_length=len(content),
    )

    assert adapter.maximum_active == 2
    assert adapter.objects[receipt.object_path] == content
    assert adapter.reads == 0
    assert receipt.stored_sha256 == hashlib.sha256(content).hexdigest()
    assert adapter.created is not None and adapter.created.placement == "immediate"
    assert "operation=retrieval_cache_hydration" in caplog.messages[-1]
    assert "raw-000001" not in caplog.messages[-1]


def test_cache_verification_and_reads_keep_exact_integrity_and_range_contracts() -> None:
    adapter = _Adapter()
    cache = _cache(adapter)
    path = "objects/aa/digest"
    content = b"first-partsecond-part"
    adapter.objects[path] = content
    adapter.revisions[path] = "version-1"
    completed = CompletedObjectReceipt(
        object_path=path,
        version_id="version-1",
        etag="entity",
        bytes=len(content),
        completed_at="2026-08-21T00:00:00Z",
    )
    parts = (
        MultipartPartReceipt(1, "one", 10, hashlib.sha256(b"first-part").hexdigest()),
        MultipartPartReceipt(2, "two", 11, hashlib.sha256(b"second-part").hexdigest()),
    )

    receipt = cache.verify_multipart_object(completed=completed, parts=parts)

    assert receipt.stored_sha256 == hashlib.sha256(content).hexdigest()
    assert (
        b"".join(
            cache.iter_object(
                object_path=path,
                version_id="version-1",
                expected_bytes=len(content),
                expected_sha256=receipt.stored_sha256,
            )
        )
        == content
    )
    assert (
        b"".join(
            cache.iter_object_range(
                object_path=path,
                version_id="version-1",
                expected_bytes=len(content),
                offset=10,
                size=11,
            )
        )
        == b"second-part"
    )

    corrupted = (parts[0], MultipartPartReceipt(2, "two", 11, "0" * 64))
    with pytest.raises(RuntimeError, match="part failed integrity"):
        cache.verify_multipart_object(completed=completed, parts=corrupted)


def test_cache_mirror_uses_deterministic_immediate_object_and_exact_deletion() -> None:
    adapter = _Adapter()
    cache = _cache(adapter)
    objects = cache.multipart_object_store(
        source_store="deep",
        collection_id=17,
        object_id="pack-000000000000",
    )
    upload = objects.create_multipart_upload(
        object_path="archives/opaque/volumes/pack-000000000000.tar.age",
        content_type="application/octet-stream",
        metadata={"riverhog-format": "riverhog-pack-volume/v1"},
    )

    assert upload.object_path.startswith("objects/")
    assert adapter.created is not None and adapter.created.placement == "immediate"
    assert adapter.created.identity_metadata["riverhog-source-store"] == "deep"

    adapter.objects[upload.object_path] = b"cached"
    adapter.revisions[upload.object_path] = "version-1"
    cache.delete(object_path=upload.object_path, version_id="version-1")

    assert adapter.deleted[0].mode == "exact_revision"
    assert adapter.deleted[0].object.revision == "version-1"

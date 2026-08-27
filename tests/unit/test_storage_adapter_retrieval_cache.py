from __future__ import annotations

import hashlib
import logging
import threading

import pytest
from riverhog_core.ports.archive_objects import CompletedObjectReceipt, WriteSegmentReceipt
from riverhog_core.stores.storage_adapter_retrieval_cache import StorageAdapterRetrievalCache
from riverhog_core.throughput import ArchiveThroughputTuning, ArchiveTransferResources
from riverhog_storage_adapter_protocol import (
    AbortIncompleteWritesRequest,
    AdapterDescriptor,
    CompletedWriteLookupRequest,
    DeleteObjectRequest,
    ImmutableObjectReceipt,
    ObjectLocator,
    ObjectPlacement,
    ObjectReadReceipt,
    ObjectReadRequest,
    ObjectReadStream,
    SmallObjectWriteRequest,
    WriteCompleteRequest,
    WriteStartRequest,
)
from riverhog_storage_adapter_protocol import (
    CompletedObjectReceipt as AdapterCompletedObjectReceipt,
)
from riverhog_storage_adapter_protocol import (
    WriteSegmentReceipt as AdapterWriteSegmentReceipt,
)
from riverhog_storage_adapter_protocol import (
    WriteSession as AdapterWriteSession,
)

_PART_BYTES = 5 * 1024 * 1024


class _Adapter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._two_segments_active = threading.Event()
        self._segments: dict[int, bytes] = {}
        self.objects: dict[str, bytes] = {}
        self.revisions: dict[str, str] = {}
        self.active = 0
        self.maximum_active = 0
        self.created: WriteStartRequest | None = None
        self.deleted: list[DeleteObjectRequest] = []
        self.reads = 0

    def descriptor(self) -> AdapterDescriptor:
        return AdapterDescriptor(
            implementation_id="fixture.cache/v1",
            implementation_version="1.0.0",
            read_mode="immediate",
            minimum_nonfinal_segment_bytes=_PART_BYTES,
            maximum_segment_bytes=32 * 1024 * 1024,
            maximum_segment_count=10_000,
        )

    def begin_write(
        self,
        request: WriteStartRequest,
    ) -> AdapterWriteSession:
        self.created = request
        return AdapterWriteSession(object_path=request.object_path, write_token="write-1")

    def write_segment(
        self,
        *,
        session: AdapterWriteSession,
        number: int,
        stored_bytes: int,
        content: bytes,
    ) -> AdapterWriteSegmentReceipt:
        assert session.write_token == "write-1"
        assert len(content) == stored_bytes
        with self._lock:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            if self.active >= 2:
                self._two_segments_active.set()
        if number <= 2:
            assert self._two_segments_active.wait(timeout=5)
        self._segments[number] = content
        with self._lock:
            self.active -= 1
        return AdapterWriteSegmentReceipt(
            number=number,
            segment_token=f"segment-{number}",
            stored_bytes=len(content),
            stored_sha256=hashlib.sha256(content).hexdigest(),
        )

    def list_segments(
        self,
        session: AdapterWriteSession,
    ) -> tuple[AdapterWriteSegmentReceipt, ...]:
        assert session.write_token == "write-1"
        return tuple(
            AdapterWriteSegmentReceipt(
                number=number,
                segment_token=f"segment-{number}",
                stored_bytes=len(content),
                stored_sha256=hashlib.sha256(content).hexdigest(),
            )
            for number, content in sorted(self._segments.items())
        )

    def complete_write(
        self,
        request: WriteCompleteRequest,
    ) -> AdapterCompletedObjectReceipt:
        content = b"".join(self._segments[current.number] for current in request.segments)
        assert len(content) == request.expected_bytes
        self.objects[request.session.object_path] = content
        self.revisions[request.session.object_path] = "version-1"
        return self._completed(
            request.session.object_path,
            content_type=request.expected_content_type,
            identity_assertions=request.required_identity_assertions,
            placement=request.expected_placement,
        )

    def find_completed_write(
        self,
        request: CompletedWriteLookupRequest,
    ) -> AdapterCompletedObjectReceipt | None:
        if request.object_path not in self.objects:
            return None
        return self._completed(
            request.object_path,
            content_type=request.expected_content_type,
            identity_assertions=request.required_identity_assertions,
            placement=request.expected_placement,
        )

    def abort_write(self, session: AdapterWriteSession) -> None:
        assert session.write_token == "write-1"

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
            verified_content_type=request.content_type,
            verified_identity_assertions=request.required_identity_assertions,
            verified_placement=request.placement,
            completed_at="2026-08-21T00:00:00.000000Z",
        )

    def read_object(self, request: ObjectReadRequest) -> ObjectReadStream:
        self.reads += 1
        content = self.objects[request.object.object_path]
        assert len(content) == request.expected_bytes
        if request.offset is not None and request.size is not None:
            content = content[request.offset : request.offset + request.size]
        return ObjectReadStream(
            receipt=ObjectReadReceipt(
                object=ObjectLocator(
                    object_path=request.object.object_path,
                    revision=self.revisions[request.object.object_path],
                ),
                total_bytes=request.expected_bytes,
                offset=request.offset or 0,
                read_bytes=len(content),
            ),
            content=iter((content,)) if content else iter(()),
        )

    def delete_object(self, request: DeleteObjectRequest) -> None:
        self.deleted.append(request)
        self.objects.pop(request.object.object_path, None)

    def abort_incomplete_writes(self, request: AbortIncompleteWritesRequest) -> int:
        assert request.object_prefix == "objects/"
        return 2

    def _completed(
        self,
        object_path: str,
        *,
        content_type: str,
        identity_assertions: dict[str, str],
        placement: ObjectPlacement,
    ) -> AdapterCompletedObjectReceipt:
        return AdapterCompletedObjectReceipt(
            object_path=object_path,
            revision=self.revisions[object_path],
            entity_token="entity-1",
            stored_bytes=len(self.objects[object_path]),
            verified_content_type=content_type,
            verified_identity_assertions=identity_assertions,
            verified_placement=placement,
            completed_at="2026-08-21T00:00:00.000000Z",
        )


def _cache(adapter: _Adapter) -> StorageAdapterRetrievalCache:
    tuning = ArchiveThroughputTuning(
        write_concurrency=2,
        upload_request_concurrency=2,
        upload_max_inflight_bytes=3 * _PART_BYTES,
    )
    return StorageAdapterRetrievalCache(
        adapter,  # type: ignore[arg-type]
        write_segment_bytes=_PART_BYTES,
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
        revision="version-1",
        entity_token="entity",
        bytes=len(content),
        completed_at="2026-08-21T00:00:00.000000Z",
    )
    segments = (
        WriteSegmentReceipt(1, "one", 10, hashlib.sha256(b"first-part").hexdigest()),
        WriteSegmentReceipt(2, "two", 11, hashlib.sha256(b"second-part").hexdigest()),
    )

    receipt = cache.verify_resumable_object(completed=completed, segments=segments)

    assert receipt.stored_sha256 == hashlib.sha256(content).hexdigest()
    assert (
        b"".join(
            cache.iter_object(
                object_path=path,
                revision="version-1",
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
                revision="version-1",
                expected_bytes=len(content),
                offset=10,
                size=11,
            )
        )
        == b"second-part"
    )

    corrupted = (segments[0], WriteSegmentReceipt(2, "two", 11, "0" * 64))
    with pytest.raises(RuntimeError, match="segment failed integrity"):
        cache.verify_resumable_object(completed=completed, segments=corrupted)


def test_cache_mirror_uses_deterministic_immediate_object_and_exact_deletion() -> None:
    adapter = _Adapter()
    cache = _cache(adapter)
    objects = cache.resumable_object_store(
        source_store="deep",
        collection_id=17,
        object_id="pack-000000000000",
    )
    session = objects.begin_write(
        object_path="archives/opaque/volumes/pack-000000000000.tar.age",
        content_type="application/octet-stream",
        metadata={"riverhog-format": "riverhog-pack-volume/v1"},
    )

    assert session.object_path.startswith("objects/")
    assert adapter.created is not None and adapter.created.placement == "immediate"
    assert adapter.created.required_identity_assertions["riverhog-source-store"] == "deep"

    adapter.objects[session.object_path] = b"cached"
    adapter.revisions[session.object_path] = "version-1"
    cache.delete(object_path=session.object_path, revision="version-1")

    assert adapter.deleted[0].mode == "exact_revision"
    assert adapter.deleted[0].object.revision == "version-1"

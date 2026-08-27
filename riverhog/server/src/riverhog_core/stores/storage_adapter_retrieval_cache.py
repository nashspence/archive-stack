from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime
from typing import Any

from riverhog_storage_adapter_protocol import (
    AbortIncompleteWritesRequest,
    CompletedWriteLookupRequest,
    DeleteObjectRequest,
    ObjectLocator,
    ObjectReadRequest,
    SmallObjectWriteRequest,
    StorageAdapterPort,
    StorageAdapterRejection,
    WriteCompleteRequest,
    WriteStartRequest,
    validated_storage_adapter,
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
from time_formats import format_utc_timestamp, utc_now

from riverhog_core.ports.archive_objects import (
    ArchiveObjectIdentityConflict,
    ArchiveResumableObjectStore,
    CompletedObjectReceipt,
    ResumableWriteConstraints,
    WriteSegmentReceipt,
    WriteSession,
)
from riverhog_core.ports.retrieval_cache import RetrievalCacheReceipt
from riverhog_core.throughput import (
    ArchiveThroughputTuning,
    ArchiveTransferResources,
    TransferTiming,
    log_transfer_timing,
)

_CACHE_FORMAT = "encrypted-archive-object-v1"


class StorageAdapterRetrievalCache:
    """Riverhog retrieval-cache semantics over one immediate-read adapter."""

    def __init__(
        self,
        adapter: StorageAdapterPort,
        *,
        write_segment_bytes: int,
        throughput_tuning: ArchiveThroughputTuning,
        transfer_resources: ArchiveTransferResources,
    ) -> None:
        validated_adapter = validated_storage_adapter(adapter)
        descriptor = validated_adapter.descriptor()
        if descriptor.read_mode != "immediate":
            raise ValueError("retrieval cache adapter must provide immediate reads")
        if write_segment_bytes < descriptor.minimum_nonfinal_segment_bytes or (
            descriptor.maximum_segment_bytes is not None
            and write_segment_bytes > descriptor.maximum_segment_bytes
        ):
            raise ValueError("retrieval cache write segment size is outside adapter limits")
        self._adapter = validated_adapter
        self._descriptor = descriptor
        self._segment_bytes = write_segment_bytes
        self._throughput = throughput_tuning
        self._resources = transfer_resources

    @staticmethod
    def _object_path(source_store: str, collection_id: int, object_id: str) -> str:
        identity = f"{source_store}\0{collection_id}\0{object_id}".encode()
        digest = hashlib.sha256(identity).hexdigest()
        return f"objects/{digest[:2]}/{digest}"

    def abort_incomplete_writes(
        self,
        *,
        initiated_before: datetime,
    ) -> int:
        if initiated_before.tzinfo is None:
            raise ValueError("retrieval cache write cutoff must be timezone-aware")
        return self._adapter.abort_incomplete_writes(
            AbortIncompleteWritesRequest(
                object_prefix="objects/",
                initiated_before=format_utc_timestamp(initiated_before),
            )
        )

    def resumable_object_store(
        self,
        *,
        source_store: str,
        collection_id: int,
        object_id: str,
    ) -> ArchiveResumableObjectStore:
        return _StorageAdapterRetrievalCacheResumableObjectStore(
            adapter=self._adapter,
            object_path=self._object_path(source_store, collection_id, object_id),
            metadata=_cache_identity(source_store, collection_id, object_id),
        )

    def verify_resumable_object(
        self,
        *,
        completed: CompletedObjectReceipt,
        segments: tuple[WriteSegmentReceipt, ...] = (),
    ) -> RetrievalCacheReceipt:
        content = self._adapter.iter_object(
            ObjectReadRequest(
                object=ObjectLocator(
                    object_path=completed.object_path,
                    revision=completed.revision,
                ),
                expected_bytes=completed.bytes,
            )
        )
        digest = hashlib.sha256()
        size = 0
        expected_segments = tuple(sorted(segments, key=lambda current: current.number))
        _validate_integrity_segments(expected_segments, expected_bytes=completed.bytes)
        segment_index = 0
        segment_size = 0
        segment_digest = hashlib.sha256()
        for chunk in content:
            digest.update(chunk)
            size += len(chunk)
            remaining = memoryview(chunk)
            while remaining and segment_index < len(expected_segments):
                expected = expected_segments[segment_index]
                accepted = min(len(remaining), expected.bytes - segment_size)
                segment_digest.update(remaining[:accepted])
                segment_size += accepted
                remaining = remaining[accepted:]
                if segment_size == expected.bytes:
                    if expected.sha256 is None or segment_digest.hexdigest() != expected.sha256:
                        raise RuntimeError(
                            "retrieval cache write segment failed integrity verification"
                        )
                    segment_index += 1
                    segment_size = 0
                    segment_digest = hashlib.sha256()
            if remaining and expected_segments:
                raise RuntimeError("retrieval cache object exceeds its write receipts")
        if size != completed.bytes or (
            expected_segments and (segment_index != len(expected_segments) or segment_size != 0)
        ):
            raise RuntimeError("retrieval cache resumable object length mismatch")
        verified_at = format_utc_timestamp(utc_now())
        return RetrievalCacheReceipt(
            object_path=completed.object_path,
            revision=completed.revision,
            stored_bytes=size,
            stored_sha256=digest.hexdigest(),
            cached_at=completed.completed_at,
            verified_at=verified_at,
        )

    def put(
        self,
        *,
        source_store: str,
        collection_id: int,
        object_id: str,
        content: Iterable[bytes],
        content_length: int,
    ) -> RetrievalCacheReceipt:
        if content_length < 0:
            raise ValueError("retrieval cache content length must be non-negative")
        if (
            self._descriptor.maximum_segment_bytes is not None
            and self._descriptor.maximum_segment_count is not None
            and content_length
            > self._descriptor.maximum_segment_bytes * self._descriptor.maximum_segment_count
        ):
            raise ValueError("retrieval cache object exceeds adapter write limits")
        object_path = self._object_path(source_store, collection_id, object_id)
        metadata = _cache_identity(source_store, collection_id, object_id)
        started = time.perf_counter()
        digest = hashlib.sha256()
        written = 0
        queue_wait_seconds = 0.0
        source_seconds = 0.0
        integrity_seconds = 0.0
        remote_seconds = 0.0

        if content_length < self._descriptor.minimum_nonfinal_segment_bytes:
            body = bytearray()
            if content_length:
                queue_wait_seconds += self._resources.upload_bytes.acquire(content_length)
            try:
                with self._resources.retrieval_requests.reserve() as retrieval_wait:
                    queue_wait_seconds += retrieval_wait
                    chunks = iter(content)
                    while True:
                        source_started = time.perf_counter()
                        try:
                            chunk = bytes(next(chunks))
                        except StopIteration:
                            source_seconds += time.perf_counter() - source_started
                            break
                        source_seconds += time.perf_counter() - source_started
                        body.extend(chunk)
                        integrity_started = time.perf_counter()
                        digest.update(chunk)
                        integrity_seconds += time.perf_counter() - integrity_started
                        written += len(chunk)
                if written != content_length:
                    raise ValueError("retrieval cache stream length changed")
                with self._resources.upload_requests.reserve() as upload_wait:
                    queue_wait_seconds += upload_wait
                    remote_started = time.perf_counter()
                    small_request = SmallObjectWriteRequest(
                        object_path=object_path,
                        content_type="application/octet-stream",
                        required_identity_assertions=metadata,
                        placement="immediate",
                        mode="replace_current",
                        stored_bytes=written,
                        stored_sha256=digest.hexdigest(),
                    )
                    receipt = self._adapter.put_small_object(small_request, bytes(body))
                    remote_seconds += time.perf_counter() - remote_started
            finally:
                if content_length:
                    self._resources.upload_bytes.release(content_length)
            revision = receipt.revision
            cached_at = receipt.completed_at
        else:
            remote_started = time.perf_counter()
            session = self._adapter.begin_write(
                WriteStartRequest(
                    object_path=object_path,
                    content_type="application/octet-stream",
                    required_identity_assertions=metadata,
                    placement="immediate",
                )
            )
            remote_seconds += time.perf_counter() - remote_started
            try:
                (
                    segments,
                    written,
                    segment_queue_seconds,
                    segment_source_seconds,
                    segment_integrity_seconds,
                    segment_remote_seconds,
                ) = self._write_segmented_content(
                    session=session,
                    content=content,
                    digest=digest,
                )
                queue_wait_seconds += segment_queue_seconds
                source_seconds += segment_source_seconds
                integrity_seconds += segment_integrity_seconds
                remote_seconds += segment_remote_seconds
                if written != content_length:
                    raise ValueError("retrieval cache stream length changed")
                remote_started = time.perf_counter()
                completion_request = WriteCompleteRequest(
                    session=session,
                    segments=segments,
                    expected_bytes=written,
                    expected_content_type="application/octet-stream",
                    required_identity_assertions=metadata,
                    expected_placement="immediate",
                )
                completed = self._adapter.complete_write(completion_request)
                remote_seconds += time.perf_counter() - remote_started
            except Exception:
                self._adapter.abort_write(session)
                raise
            revision = completed.revision
            cached_at = completed.completed_at

        log_transfer_timing(
            TransferTiming(
                operation="retrieval_cache_hydration",
                identity=object_path,
                plaintext_bytes=content_length,
                stored_bytes=written,
                queue_wait_seconds=queue_wait_seconds,
                source_seconds=source_seconds,
                integrity_seconds=integrity_seconds,
                crypto_seconds=0.0,
                processing_seconds=0.0,
                remote_seconds=remote_seconds,
                checkpoint_seconds=0.0,
                elapsed_seconds=time.perf_counter() - started,
            )
        )
        current = format_utc_timestamp(utc_now())
        return RetrievalCacheReceipt(
            object_path=object_path,
            revision=revision,
            stored_bytes=written,
            stored_sha256=digest.hexdigest(),
            cached_at=cached_at,
            verified_at=current,
        )

    def _write_segmented_content(
        self,
        *,
        session: AdapterWriteSession,
        content: Iterable[bytes],
        digest: Any,
    ) -> tuple[
        tuple[AdapterWriteSegmentReceipt, ...],
        int,
        float,
        float,
        float,
        float,
    ]:
        worker_count = self._throughput.write_concurrency
        window = worker_count * 2
        chunks = iter(content)
        buffer = bytearray()
        source_done = False
        written = 0
        next_segment_number = 1
        pending: dict[Future[tuple[AdapterWriteSegmentReceipt, float, float]], int] = {}
        completed: dict[int, AdapterWriteSegmentReceipt] = {}
        queue_wait_seconds = 0.0
        source_seconds = 0.0
        integrity_seconds = 0.0
        remote_seconds = 0.0

        def next_segment() -> bytes | None:
            nonlocal integrity_seconds, source_done, source_seconds, written
            while len(buffer) < self._segment_bytes and not source_done:
                source_started = time.perf_counter()
                try:
                    chunk = bytes(next(chunks))
                except StopIteration:
                    source_seconds += time.perf_counter() - source_started
                    source_done = True
                    break
                source_seconds += time.perf_counter() - source_started
                if chunk:
                    buffer.extend(chunk)
                    integrity_started = time.perf_counter()
                    digest.update(chunk)
                    integrity_seconds += time.perf_counter() - integrity_started
                    written += len(chunk)
            if len(buffer) >= self._segment_bytes:
                body = bytes(buffer[: self._segment_bytes])
                del buffer[: self._segment_bytes]
                return body
            if source_done and buffer:
                body = bytes(buffer)
                buffer.clear()
                return body
            return None

        with self._resources.retrieval_requests.reserve() as retrieval_wait:
            queue_wait_seconds += retrieval_wait
            with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="riverhog-retrieval-cache-segment",
            ) as executor:

                def fill() -> None:
                    nonlocal next_segment_number, queue_wait_seconds
                    while len(pending) < window:
                        body = next_segment()
                        if body is None:
                            return
                        if (
                            self._descriptor.maximum_segment_count is not None
                            and next_segment_number > self._descriptor.maximum_segment_count
                        ):
                            raise ValueError("retrieval cache object exceeds adapter segment count")
                        reserved = len(body)
                        queue_wait_seconds += self._resources.upload_bytes.acquire(reserved)
                        segment_number = next_segment_number
                        next_segment_number += 1
                        try:
                            future = executor.submit(
                                self._write_segment,
                                session=session,
                                segment_number=segment_number,
                                body=body,
                            )
                        except BaseException:
                            self._resources.upload_bytes.release(reserved)
                            raise

                        def release_buffer(
                            _future: Future[tuple[AdapterWriteSegmentReceipt, float, float]],
                            amount: int = reserved,
                        ) -> None:
                            self._resources.upload_bytes.release(amount)

                        future.add_done_callback(release_buffer)
                        pending[future] = segment_number

                fill()
                while pending:
                    done, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
                    for future in done:
                        segment_number = pending.pop(future)
                        receipt, write_wait, write_seconds = future.result()
                        completed[segment_number] = receipt
                        queue_wait_seconds += write_wait
                        remote_seconds += write_seconds
                    fill()

        return (
            tuple(completed[number] for number in sorted(completed)),
            written,
            queue_wait_seconds,
            source_seconds,
            integrity_seconds,
            remote_seconds,
        )

    def _write_segment(
        self,
        *,
        session: AdapterWriteSession,
        segment_number: int,
        body: bytes,
    ) -> tuple[AdapterWriteSegmentReceipt, float, float]:
        with self._resources.upload_requests.reserve() as write_wait:
            remote_started = time.perf_counter()
            receipt = self._adapter.write_segment(
                session=session,
                number=segment_number,
                stored_bytes=len(body),
                content=body,
            )
            remote_seconds = time.perf_counter() - remote_started
        return receipt, write_wait, remote_seconds

    def iter_object(
        self,
        *,
        object_path: str,
        revision: str | None,
        expected_bytes: int,
        expected_sha256: str,
    ) -> Iterator[bytes]:
        digest = hashlib.sha256()
        size = 0
        for chunk in self._adapter.iter_object(
            ObjectReadRequest(
                object=ObjectLocator(object_path=object_path, revision=revision),
                expected_bytes=expected_bytes,
            )
        ):
            digest.update(chunk)
            size += len(chunk)
            yield chunk
        if size != expected_bytes or digest.hexdigest() != expected_sha256:
            raise RuntimeError("retrieval cache object does not match its verified record")

    def iter_object_range(
        self,
        *,
        object_path: str,
        revision: str | None,
        expected_bytes: int,
        offset: int,
        size: int,
    ) -> Iterator[bytes]:
        return self._adapter.iter_object(
            ObjectReadRequest(
                object=ObjectLocator(object_path=object_path, revision=revision),
                expected_bytes=expected_bytes,
                offset=offset,
                size=size,
            )
        )

    def delete(self, *, object_path: str, revision: str | None) -> None:
        self._adapter.delete_object(
            DeleteObjectRequest(
                object=ObjectLocator(object_path=object_path, revision=revision),
                mode="exact_revision" if revision is not None else "current",
            )
        )


class _StorageAdapterRetrievalCacheResumableObjectStore:
    def __init__(
        self,
        *,
        adapter: StorageAdapterPort,
        object_path: str,
        metadata: dict[str, str],
    ) -> None:
        self._adapter = validated_storage_adapter(adapter)
        self._object_path = object_path
        self._metadata = metadata

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
        content_type: str,
        metadata: dict[str, str],
    ) -> WriteSession:
        _ = object_path
        session = self._adapter.begin_write(
            WriteStartRequest(
                object_path=self._object_path,
                content_type=content_type,
                required_identity_assertions=self._cache_metadata(metadata),
                placement="immediate",
            )
        )
        return WriteSession(session.object_path, session.write_token)

    def write_segment(
        self,
        *,
        session: WriteSession,
        number: int,
        content: bytes,
    ) -> WriteSegmentReceipt:
        self._require_path(session.object_path)
        receipt = self._adapter.write_segment(
            session=AdapterWriteSession(
                object_path=session.object_path,
                write_token=session.write_token,
            ),
            number=number,
            stored_bytes=len(content),
            content=content,
        )
        return WriteSegmentReceipt(
            receipt.number,
            receipt.segment_token,
            receipt.stored_bytes,
            receipt.stored_sha256,
        )

    def list_segments(self, *, session: WriteSession) -> tuple[WriteSegmentReceipt, ...]:
        self._require_path(session.object_path)
        return tuple(
            WriteSegmentReceipt(
                current.number,
                current.segment_token,
                current.stored_bytes,
                current.stored_sha256,
            )
            for current in self._adapter.list_segments(
                AdapterWriteSession(
                    object_path=session.object_path,
                    write_token=session.write_token,
                )
            ).segments
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
        self._require_path(session.object_path)
        request = WriteCompleteRequest(
            session=AdapterWriteSession(
                object_path=session.object_path,
                write_token=session.write_token,
            ),
            segments=tuple(
                AdapterWriteSegmentReceipt(
                    number=current.number,
                    segment_token=current.segment_token,
                    stored_bytes=current.bytes,
                    stored_sha256=current.sha256,
                )
                for current in segments
            ),
            expected_bytes=expected_bytes,
            expected_content_type=expected_content_type,
            required_identity_assertions=self._cache_metadata(expected_metadata),
            expected_placement="immediate",
        )
        receipt = self._adapter.complete_write(request)
        return _completed(receipt)

    def find_completed_write(
        self,
        *,
        object_path: str,
        expected_content_type: str,
        expected_metadata: dict[str, str],
    ) -> CompletedObjectReceipt | None:
        _ = object_path
        request = CompletedWriteLookupRequest(
            object_path=self._object_path,
            expected_content_type=expected_content_type,
            required_identity_assertions=self._cache_metadata(expected_metadata),
            expected_placement="immediate",
        )
        try:
            receipt = self._adapter.find_completed_write(request)
        except StorageAdapterRejection as exc:
            if exc.code == "identity_conflict":
                raise ArchiveObjectIdentityConflict(str(exc)) from exc
            raise
        if receipt is None:
            return None
        return _completed(receipt)

    def abort_write(self, *, session: WriteSession) -> None:
        self._require_path(session.object_path)
        self._adapter.abort_write(
            AdapterWriteSession(
                object_path=session.object_path,
                write_token=session.write_token,
            )
        )

    def _require_path(self, object_path: str) -> None:
        if object_path != self._object_path:
            raise ValueError("retrieval cache resumable object path changed")

    def _cache_metadata(self, source_metadata: dict[str, str]) -> dict[str, str]:
        if not source_metadata:
            return dict(self._metadata)
        normalized = {str(key).casefold(): str(value) for key, value in source_metadata.items()}
        source_identity = hashlib.sha256(
            json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return {**self._metadata, "riverhog-source-metadata-sha256": source_identity}


def _cache_identity(source_store: str, collection_id: int, object_id: str) -> dict[str, str]:
    return {
        "riverhog-cache-format": _CACHE_FORMAT,
        "riverhog-source-store": source_store,
        "riverhog-source-identity": hashlib.sha256(
            f"{collection_id}\0{object_id}".encode()
        ).hexdigest(),
    }


def _completed(receipt: AdapterCompletedObjectReceipt) -> CompletedObjectReceipt:
    return CompletedObjectReceipt(
        object_path=receipt.object_path,
        revision=receipt.revision,
        entity_token=receipt.entity_token,
        bytes=receipt.stored_bytes,
        completed_at=receipt.completed_at,
    )


def _validate_integrity_segments(
    segments: tuple[WriteSegmentReceipt, ...],
    *,
    expected_bytes: int,
) -> None:
    if not segments:
        return
    if (
        tuple(current.number for current in segments) != tuple(range(1, len(segments) + 1))
        or sum(current.bytes for current in segments) != expected_bytes
        or any(current.sha256 is None or len(current.sha256) != 64 for current in segments)
    ):
        raise ValueError("retrieval cache write integrity receipts are invalid")


__all__ = ["StorageAdapterRetrievalCache"]

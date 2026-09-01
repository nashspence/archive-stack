from __future__ import annotations

import hashlib
import time
from collections.abc import Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Any

from riverhog_storage_adapter_protocol import (
    CompletedObjectReceipt as AdapterCompletedObjectReceipt,
)
from riverhog_storage_adapter_protocol import (
    CompletedWriteLookupRequest,
    DeleteObjectRequest,
    ObjectLocator,
    ObjectReadRequest,
    StorageAdapterPort,
    StorageAdapterRejection,
    WriteCompleteRequest,
    WriteStartRequest,
    validated_storage_adapter,
)
from riverhog_storage_adapter_protocol import (
    WriteSegmentReceipt as AdapterWriteSegmentReceipt,
)
from riverhog_storage_adapter_protocol import WriteSession as AdapterWriteSession
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
    """One named immediate-read adapter used as a cache-placement candidate."""

    def __init__(
        self,
        name: str,
        adapter: StorageAdapterPort,
        *,
        write_segment_bytes: int,
        throughput_tuning: ArchiveThroughputTuning,
        transfer_resources: ArchiveTransferResources,
    ) -> None:
        if not name.strip():
            raise ValueError("retrieval cache store name is required")
        validated_adapter = validated_storage_adapter(adapter)
        descriptor = validated_adapter.descriptor()
        if descriptor.read_mode != "immediate":
            raise ValueError("retrieval cache adapter must provide immediate reads")
        if write_segment_bytes < descriptor.minimum_nonfinal_segment_bytes or (
            descriptor.maximum_segment_bytes is not None
            and write_segment_bytes > descriptor.maximum_segment_bytes
        ):
            raise ValueError("retrieval cache write segment size is outside adapter limits")
        self.name = name
        self._adapter = validated_adapter
        self._descriptor = descriptor
        self._segment_bytes = write_segment_bytes
        self._throughput = throughput_tuning
        self._resources = transfer_resources

    @staticmethod
    def object_path(source_store: str, collection_id: int, object_id: str) -> str:
        identity = f"{source_store}\0{collection_id}\0{object_id}".encode()
        digest = hashlib.sha256(identity).hexdigest()
        return f"objects/{digest[:2]}/{digest}"

    def write_constraints(self) -> ResumableWriteConstraints:
        return ResumableWriteConstraints(
            minimum_nonfinal_segment_bytes=self._descriptor.minimum_nonfinal_segment_bytes,
            maximum_segment_bytes=self._descriptor.maximum_segment_bytes,
            maximum_segment_count=self._descriptor.maximum_segment_count,
        )

    def begin_population(
        self,
        *,
        source_store: str,
        collection_id: int,
        object_id: str,
        expected_bytes: int,
    ) -> WriteSession:
        session = self._adapter.begin_write(
            WriteStartRequest(
                object_path=self.object_path(source_store, collection_id, object_id),
                expected_bytes=expected_bytes,
                content_type="application/octet-stream",
                required_identity_assertions=_cache_identity(
                    source_store,
                    collection_id,
                    object_id,
                ),
                placement="immediate",
            )
        )
        return _write_session(session)

    def find_completed_population(
        self,
        *,
        source_store: str,
        collection_id: int,
        object_id: str,
        expected_bytes: int,
    ) -> CompletedObjectReceipt | None:
        request = CompletedWriteLookupRequest(
            object_path=self.object_path(source_store, collection_id, object_id),
            expected_bytes=expected_bytes,
            expected_content_type="application/octet-stream",
            required_identity_assertions=_cache_identity(
                source_store,
                collection_id,
                object_id,
            ),
            expected_placement="immediate",
        )
        try:
            receipt = self._adapter.find_completed_write(request)
        except StorageAdapterRejection as exc:
            if exc.code == "identity_conflict":
                raise ArchiveObjectIdentityConflict(str(exc)) from exc
            raise
        return None if receipt is None else _completed(receipt)

    def populate(
        self,
        *,
        session: WriteSession,
        source_store: str,
        collection_id: int,
        object_id: str,
        content: Iterable[bytes],
    ) -> RetrievalCacheReceipt:
        if session.expected_bytes < 1:
            raise ValueError("retrieval cache content length must be positive")
        started = time.perf_counter()
        digest = hashlib.sha256()
        existing = self._adapter.list_segments(_adapter_session(session)).segments
        (
            segments,
            written,
            queue_wait_seconds,
            source_seconds,
            integrity_seconds,
            remote_seconds,
        ) = self._write_segmented_content(
            session=_adapter_session(session),
            content=content,
            digest=digest,
            existing=existing,
        )
        if written != session.expected_bytes:
            raise ValueError("retrieval cache stream length changed")
        remote_started = time.perf_counter()
        completed = self._adapter.complete_write(
            WriteCompleteRequest(
                session=_adapter_session(session),
                segments=segments,
                expected_bytes=session.expected_bytes,
                expected_content_type="application/octet-stream",
                required_identity_assertions=_cache_identity(
                    source_store,
                    collection_id,
                    object_id,
                ),
                expected_placement="immediate",
            )
        )
        remote_seconds += time.perf_counter() - remote_started
        log_transfer_timing(
            TransferTiming(
                operation="retrieval_cache_hydration",
                identity=session.object_path,
                plaintext_bytes=session.expected_bytes,
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
        return self.receipt(completed=_completed(completed), stored_sha256=digest.hexdigest())

    def receipt(
        self,
        *,
        completed: CompletedObjectReceipt,
        stored_sha256: str | None,
    ) -> RetrievalCacheReceipt:
        return RetrievalCacheReceipt(
            cache_store=self.name,
            object_path=completed.object_path,
            revision=completed.revision,
            stored_bytes=completed.bytes,
            stored_sha256=stored_sha256,
            cached_at=completed.completed_at,
            verified_at=format_utc_timestamp(utc_now()),
        )

    def resumable_object_store(
        self,
        *,
        source_store: str,
        collection_id: int,
        object_id: str,
    ) -> ArchiveResumableObjectStore:
        return _StorageAdapterRetrievalCacheResumableObjectStore(
            cache=self,
            object_path=self.object_path(source_store, collection_id, object_id),
            metadata=_cache_identity(source_store, collection_id, object_id),
        )

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

    def delete(self, *, object_path: str, revision: str | None) -> None:
        self._adapter.delete_object(
            DeleteObjectRequest(
                object=ObjectLocator(object_path=object_path, revision=revision),
                mode="exact_revision" if revision is not None else "current",
            )
        )

    def _write_segmented_content(
        self,
        *,
        session: AdapterWriteSession,
        content: Iterable[bytes],
        digest: Any,
        existing: tuple[AdapterWriteSegmentReceipt, ...],
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
        existing_by_number = {current.number: current for current in existing}
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
                        current_number = next_segment_number
                        next_segment_number += 1
                        prior = existing_by_number.get(current_number)
                        if prior is not None:
                            if prior.stored_bytes != len(body) or (
                                prior.stored_sha256 is not None
                                and prior.stored_sha256 != hashlib.sha256(body).hexdigest()
                            ):
                                raise RuntimeError(
                                    "retrieval cache resumed segment differs from its source"
                                )
                            completed[current_number] = prior
                            continue
                        reserved = len(body)
                        queue_wait_seconds += self._resources.upload_bytes.acquire(reserved)
                        try:
                            future = executor.submit(
                                self._write_segment,
                                session=session,
                                segment_number=current_number,
                                body=body,
                            )
                        except BaseException:
                            self._resources.upload_bytes.release(reserved)
                            raise

                        def release_reserved_bytes(
                            _future: Future[tuple[AdapterWriteSegmentReceipt, float, float]],
                            *,
                            amount: int = reserved,
                        ) -> None:
                            self._resources.upload_bytes.release(amount)

                        future.add_done_callback(release_reserved_bytes)
                        pending[future] = current_number

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

        if set(existing_by_number) - set(completed):
            raise RuntimeError("retrieval cache write contains unexpected resumed segments")
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


class _StorageAdapterRetrievalCacheResumableObjectStore:
    def __init__(
        self,
        *,
        cache: StorageAdapterRetrievalCache,
        object_path: str,
        metadata: dict[str, str],
    ) -> None:
        self._cache = cache
        self._adapter = cache._adapter
        self._object_path = object_path
        self._metadata = metadata

    def write_constraints(self) -> ResumableWriteConstraints:
        return self._cache.write_constraints()

    def begin_write(
        self,
        *,
        object_path: str,
        expected_bytes: int,
        content_type: str,
        metadata: dict[str, str],
    ) -> WriteSession:
        _ = object_path, content_type, metadata
        session = self._adapter.begin_write(
            WriteStartRequest(
                object_path=self._object_path,
                expected_bytes=expected_bytes,
                content_type="application/octet-stream",
                required_identity_assertions=self._metadata,
                placement="immediate",
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
        self._require_session(session)
        return _write_segment(
            self._adapter.write_segment(
                session=_adapter_session(session),
                number=number,
                stored_bytes=len(content),
                content=content,
            )
        )

    def list_segments(self, *, session: WriteSession) -> tuple[WriteSegmentReceipt, ...]:
        self._require_session(session)
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
        self._require_session(session)
        _ = expected_content_type, expected_metadata
        if expected_bytes != session.expected_bytes:
            raise ValueError("retrieval cache admitted byte length changed")
        receipt = self._adapter.complete_write(
            WriteCompleteRequest(
                session=_adapter_session(session),
                segments=tuple(_adapter_segment(current) for current in segments),
                expected_bytes=expected_bytes,
                expected_content_type="application/octet-stream",
                required_identity_assertions=self._metadata,
                expected_placement="immediate",
            )
        )
        return self._completed_with_cache(_completed(receipt))

    def find_completed_write(
        self,
        *,
        object_path: str,
        expected_bytes: int,
        expected_content_type: str,
        expected_metadata: dict[str, str],
    ) -> CompletedObjectReceipt | None:
        _ = object_path, expected_content_type, expected_metadata
        request = CompletedWriteLookupRequest(
            object_path=self._object_path,
            expected_bytes=expected_bytes,
            expected_content_type="application/octet-stream",
            required_identity_assertions=self._metadata,
            expected_placement="immediate",
        )
        try:
            receipt = self._adapter.find_completed_write(request)
        except Exception as exc:
            if getattr(exc, "code", None) == "identity_conflict":
                raise ArchiveObjectIdentityConflict(str(exc)) from exc
            raise
        return None if receipt is None else self._completed_with_cache(_completed(receipt))

    def abort_write(self, *, session: WriteSession) -> None:
        self._require_session(session)
        self._adapter.abort_write(_adapter_session(session))

    def _completed_with_cache(self, completed: CompletedObjectReceipt) -> CompletedObjectReceipt:
        return CompletedObjectReceipt(
            object_path=completed.object_path,
            revision=completed.revision,
            entity_token=completed.entity_token,
            bytes=completed.bytes,
            completed_at=completed.completed_at,
            retrieval_cache=self._cache.receipt(completed=completed, stored_sha256=None),
        )

    def _require_session(self, session: WriteSession) -> None:
        if session.object_path != self._object_path:
            raise ValueError("retrieval cache resumable object path changed")


def _cache_identity(source_store: str, collection_id: int, object_id: str) -> dict[str, str]:
    return {
        "riverhog-cache-format": _CACHE_FORMAT,
        "riverhog-source-store": source_store,
        "riverhog-source-identity": hashlib.sha256(
            f"{collection_id}\0{object_id}".encode()
        ).hexdigest(),
    }


def _adapter_session(session: WriteSession) -> AdapterWriteSession:
    return AdapterWriteSession(
        object_path=session.object_path,
        expected_bytes=session.expected_bytes,
        write_token=session.write_token,
    )


def _write_session(session: AdapterWriteSession) -> WriteSession:
    return WriteSession(session.object_path, session.write_token, session.expected_bytes)


def _adapter_segment(segment: WriteSegmentReceipt) -> AdapterWriteSegmentReceipt:
    return AdapterWriteSegmentReceipt(
        number=segment.number,
        segment_token=segment.segment_token,
        stored_bytes=segment.bytes,
        stored_sha256=segment.sha256,
    )


def _write_segment(segment: AdapterWriteSegmentReceipt) -> WriteSegmentReceipt:
    return WriteSegmentReceipt(
        segment.number,
        segment.segment_token,
        segment.stored_bytes,
        segment.stored_sha256,
    )


def _completed(receipt: AdapterCompletedObjectReceipt) -> CompletedObjectReceipt:
    return CompletedObjectReceipt(
        object_path=receipt.object_path,
        revision=receipt.revision,
        entity_token=receipt.entity_token,
        bytes=receipt.stored_bytes,
        completed_at=receipt.completed_at,
    )


__all__ = ["StorageAdapterRetrievalCache"]

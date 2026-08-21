from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime
from typing import Any

from riverhog_storage_adapter_protocol import (
    AbortIncompleteUploadsRequest,
    DeleteObjectRequest,
    MultipartCompleteRequest,
    MultipartCreateRequest,
    MultipartHeadRequest,
    ObjectLocator,
    ObjectReadRequest,
    SmallObjectWriteRequest,
    StorageAdapterPort,
    StorageAdapterRejection,
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
from time_formats import format_utc_timestamp, utc_now

from riverhog_core.ports.archive_objects import (
    ArchiveMultipartObjectStore,
    ArchiveObjectIdentityConflict,
    CompletedObjectReceipt,
    MultipartPartReceipt,
    MultipartUpload,
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
        multipart_part_bytes: int,
        throughput_tuning: ArchiveThroughputTuning,
        transfer_resources: ArchiveTransferResources,
    ) -> None:
        descriptor = adapter.descriptor()
        if descriptor.read_mode != "immediate":
            raise ValueError("retrieval cache adapter must provide immediate reads")
        if not (
            descriptor.minimum_nonfinal_part_bytes
            <= multipart_part_bytes
            <= descriptor.maximum_part_bytes
        ):
            raise ValueError("retrieval cache multipart part size is outside adapter limits")
        self._adapter = adapter
        self._descriptor = descriptor
        self._part_bytes = multipart_part_bytes
        self._throughput = throughput_tuning
        self._resources = transfer_resources

    @staticmethod
    def _object_path(source_store: str, collection_id: int, object_id: str) -> str:
        identity = f"{source_store}\0{collection_id}\0{object_id}".encode()
        digest = hashlib.sha256(identity).hexdigest()
        return f"objects/{digest[:2]}/{digest}"

    def abort_incomplete_multipart_uploads(
        self,
        *,
        initiated_before: datetime,
    ) -> int:
        if initiated_before.tzinfo is None:
            raise ValueError("retrieval cache multipart cutoff must be timezone-aware")
        return self._adapter.abort_incomplete_uploads(
            AbortIncompleteUploadsRequest(
                object_prefix="objects/",
                initiated_before=format_utc_timestamp(initiated_before),
            )
        )

    def multipart_object_store(
        self,
        *,
        source_store: str,
        collection_id: int,
        object_id: str,
    ) -> ArchiveMultipartObjectStore:
        return _StorageAdapterRetrievalCacheMultipartObjectStore(
            adapter=self._adapter,
            object_path=self._object_path(source_store, collection_id, object_id),
            metadata=_cache_identity(source_store, collection_id, object_id),
        )

    def verify_multipart_object(
        self,
        *,
        completed: CompletedObjectReceipt,
        parts: tuple[MultipartPartReceipt, ...] = (),
    ) -> RetrievalCacheReceipt:
        content = self._adapter.iter_object(
            ObjectReadRequest(
                object=ObjectLocator(
                    object_path=completed.object_path,
                    revision=completed.version_id,
                ),
                expected_bytes=completed.bytes,
            )
        )
        digest = hashlib.sha256()
        size = 0
        expected_parts = tuple(sorted(parts, key=lambda current: current.number))
        _validate_integrity_parts(expected_parts, expected_bytes=completed.bytes)
        part_index = 0
        part_size = 0
        part_digest = hashlib.sha256()
        for chunk in content:
            digest.update(chunk)
            size += len(chunk)
            remaining = memoryview(chunk)
            while remaining and part_index < len(expected_parts):
                expected = expected_parts[part_index]
                accepted = min(len(remaining), expected.bytes - part_size)
                part_digest.update(remaining[:accepted])
                part_size += accepted
                remaining = remaining[accepted:]
                if part_size == expected.bytes:
                    if expected.sha256 is None or part_digest.hexdigest() != expected.sha256:
                        raise RuntimeError(
                            "retrieval cache multipart part failed integrity verification"
                        )
                    part_index += 1
                    part_size = 0
                    part_digest = hashlib.sha256()
            if remaining and expected_parts:
                raise RuntimeError("retrieval cache object exceeds its multipart receipts")
        if size != completed.bytes or (
            expected_parts and (part_index != len(expected_parts) or part_size != 0)
        ):
            raise RuntimeError("retrieval cache multipart object length mismatch")
        verified_at = format_utc_timestamp(utc_now())
        return RetrievalCacheReceipt(
            object_path=completed.object_path,
            version_id=completed.version_id,
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
        maximum = self._descriptor.maximum_part_bytes * self._descriptor.maximum_part_count
        if content_length > maximum:
            raise ValueError("retrieval cache object exceeds adapter multipart limits")
        object_path = self._object_path(source_store, collection_id, object_id)
        metadata = _cache_identity(source_store, collection_id, object_id)
        started = time.perf_counter()
        digest = hashlib.sha256()
        written = 0
        queue_wait_seconds = 0.0
        source_seconds = 0.0
        integrity_seconds = 0.0
        remote_seconds = 0.0

        if content_length < self._descriptor.minimum_nonfinal_part_bytes:
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
                    receipt = self._adapter.put_small_object(
                        SmallObjectWriteRequest(
                            object_path=object_path,
                            content_type="application/octet-stream",
                            identity_metadata=metadata,
                            placement="immediate",
                            mode="replace_current",
                            stored_bytes=written,
                            stored_sha256=digest.hexdigest(),
                        ),
                        bytes(body),
                    )
                    remote_seconds += time.perf_counter() - remote_started
            finally:
                if content_length:
                    self._resources.upload_bytes.release(content_length)
            version_id = receipt.revision
            cached_at = receipt.completed_at
        else:
            remote_started = time.perf_counter()
            upload = self._adapter.create_multipart_upload(
                MultipartCreateRequest(
                    object_path=object_path,
                    content_type="application/octet-stream",
                    identity_metadata=metadata,
                    placement="immediate",
                )
            )
            remote_seconds += time.perf_counter() - remote_started
            try:
                (
                    parts,
                    written,
                    part_queue_seconds,
                    part_source_seconds,
                    part_integrity_seconds,
                    part_remote_seconds,
                ) = self._upload_multipart_content(
                    upload=upload,
                    content=content,
                    digest=digest,
                )
                queue_wait_seconds += part_queue_seconds
                source_seconds += part_source_seconds
                integrity_seconds += part_integrity_seconds
                remote_seconds += part_remote_seconds
                if written != content_length:
                    raise ValueError("retrieval cache stream length changed")
                remote_started = time.perf_counter()
                completed = self._adapter.complete_multipart_upload(
                    MultipartCompleteRequest(
                        upload=upload,
                        parts=parts,
                        expected_bytes=written,
                        expected_identity_metadata=metadata,
                        expected_placement="immediate",
                    )
                )
                remote_seconds += time.perf_counter() - remote_started
            except Exception:
                self._adapter.abort_multipart_upload(upload)
                raise
            version_id = completed.revision
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
            version_id=version_id,
            stored_bytes=written,
            stored_sha256=digest.hexdigest(),
            cached_at=cached_at,
            verified_at=current,
        )

    def _upload_multipart_content(
        self,
        *,
        upload: AdapterMultipartUpload,
        content: Iterable[bytes],
        digest: Any,
    ) -> tuple[
        tuple[AdapterMultipartPartReceipt, ...],
        int,
        float,
        float,
        float,
        float,
    ]:
        worker_count = self._throughput.multipart_concurrency
        window = worker_count * 2
        chunks = iter(content)
        buffer = bytearray()
        source_done = False
        written = 0
        next_part_number = 1
        pending: dict[Future[tuple[AdapterMultipartPartReceipt, float, float]], int] = {}
        completed: dict[int, AdapterMultipartPartReceipt] = {}
        queue_wait_seconds = 0.0
        source_seconds = 0.0
        integrity_seconds = 0.0
        remote_seconds = 0.0

        def next_part() -> bytes | None:
            nonlocal integrity_seconds, source_done, source_seconds, written
            while len(buffer) < self._part_bytes and not source_done:
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
            if len(buffer) >= self._part_bytes:
                body = bytes(buffer[: self._part_bytes])
                del buffer[: self._part_bytes]
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
                thread_name_prefix="riverhog-retrieval-cache-part",
            ) as executor:

                def fill() -> None:
                    nonlocal next_part_number, queue_wait_seconds
                    while len(pending) < window:
                        body = next_part()
                        if body is None:
                            return
                        if next_part_number > self._descriptor.maximum_part_count:
                            raise ValueError("retrieval cache object exceeds adapter part count")
                        reserved = len(body)
                        queue_wait_seconds += self._resources.upload_bytes.acquire(reserved)
                        part_number = next_part_number
                        next_part_number += 1
                        try:
                            future = executor.submit(
                                self._upload_part,
                                upload=upload,
                                part_number=part_number,
                                body=body,
                            )
                        except BaseException:
                            self._resources.upload_bytes.release(reserved)
                            raise

                        def release_buffer(
                            _future: Future[tuple[AdapterMultipartPartReceipt, float, float]],
                            amount: int = reserved,
                        ) -> None:
                            self._resources.upload_bytes.release(amount)

                        future.add_done_callback(release_buffer)
                        pending[future] = part_number

                fill()
                while pending:
                    done, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
                    for future in done:
                        part_number = pending.pop(future)
                        receipt, upload_wait, upload_seconds = future.result()
                        completed[part_number] = receipt
                        queue_wait_seconds += upload_wait
                        remote_seconds += upload_seconds
                    fill()

        return (
            tuple(completed[number] for number in sorted(completed)),
            written,
            queue_wait_seconds,
            source_seconds,
            integrity_seconds,
            remote_seconds,
        )

    def _upload_part(
        self,
        *,
        upload: AdapterMultipartUpload,
        part_number: int,
        body: bytes,
    ) -> tuple[AdapterMultipartPartReceipt, float, float]:
        with self._resources.upload_requests.reserve() as upload_wait:
            remote_started = time.perf_counter()
            receipt = self._adapter.upload_part(
                upload=upload,
                number=part_number,
                content=body,
            )
            remote_seconds = time.perf_counter() - remote_started
        return receipt, upload_wait, remote_seconds

    def iter_object(
        self,
        *,
        object_path: str,
        version_id: str | None,
        expected_bytes: int,
        expected_sha256: str,
    ) -> Iterator[bytes]:
        digest = hashlib.sha256()
        size = 0
        for chunk in self._adapter.iter_object(
            ObjectReadRequest(
                object=ObjectLocator(object_path=object_path, revision=version_id),
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

    def delete(self, *, object_path: str, version_id: str | None) -> None:
        self._adapter.delete_object(
            DeleteObjectRequest(
                object=ObjectLocator(object_path=object_path, revision=version_id),
                mode="exact_revision" if version_id is not None else "current",
            )
        )


class _StorageAdapterRetrievalCacheMultipartObjectStore:
    def __init__(
        self,
        *,
        adapter: StorageAdapterPort,
        object_path: str,
        metadata: dict[str, str],
    ) -> None:
        self._adapter = adapter
        self._object_path = object_path
        self._metadata = metadata

    def create_multipart_upload(
        self,
        *,
        object_path: str,
        content_type: str,
        metadata: dict[str, str],
    ) -> MultipartUpload:
        _ = object_path
        upload = self._adapter.create_multipart_upload(
            MultipartCreateRequest(
                object_path=self._object_path,
                content_type=content_type,
                identity_metadata=self._cache_metadata(metadata),
                placement="immediate",
            )
        )
        return MultipartUpload(upload.object_path, upload.upload_id)

    def upload_part(
        self,
        *,
        upload: MultipartUpload,
        number: int,
        content: bytes,
    ) -> MultipartPartReceipt:
        self._require_path(upload.object_path)
        receipt = self._adapter.upload_part(
            upload=AdapterMultipartUpload(
                object_path=upload.object_path,
                upload_id=upload.upload_id,
            ),
            number=number,
            content=content,
        )
        return MultipartPartReceipt(
            receipt.number,
            receipt.part_token,
            receipt.stored_bytes,
            receipt.stored_sha256,
        )

    def list_parts(self, *, upload: MultipartUpload) -> tuple[MultipartPartReceipt, ...]:
        self._require_path(upload.object_path)
        return tuple(
            MultipartPartReceipt(
                current.number,
                current.part_token,
                current.stored_bytes,
                current.stored_sha256,
            )
            for current in self._adapter.list_parts(
                AdapterMultipartUpload(
                    object_path=upload.object_path,
                    upload_id=upload.upload_id,
                )
            )
        )

    def complete_multipart_upload(
        self,
        *,
        upload: MultipartUpload,
        parts: tuple[MultipartPartReceipt, ...],
        expected_bytes: int,
        expected_metadata: dict[str, str],
    ) -> CompletedObjectReceipt:
        self._require_path(upload.object_path)
        receipt = self._adapter.complete_multipart_upload(
            MultipartCompleteRequest(
                upload=AdapterMultipartUpload(
                    object_path=upload.object_path,
                    upload_id=upload.upload_id,
                ),
                parts=tuple(
                    AdapterMultipartPartReceipt(
                        number=current.number,
                        part_token=current.etag,
                        stored_bytes=current.bytes,
                        stored_sha256=current.sha256,
                    )
                    for current in parts
                ),
                expected_bytes=expected_bytes,
                expected_identity_metadata=self._cache_metadata(expected_metadata),
                expected_placement="immediate",
            )
        )
        return _completed(receipt)

    def head_completed_object(
        self,
        *,
        object_path: str,
        expected_metadata: dict[str, str],
    ) -> CompletedObjectReceipt | None:
        _ = object_path
        try:
            receipt = self._adapter.head_completed_object(
                MultipartHeadRequest(
                    object_path=self._object_path,
                    expected_identity_metadata=self._cache_metadata(expected_metadata),
                    expected_placement="immediate",
                )
            )
        except StorageAdapterRejection as exc:
            if exc.code == "identity_conflict":
                raise ArchiveObjectIdentityConflict(str(exc)) from exc
            raise
        return _completed(receipt) if receipt is not None else None

    def abort_multipart_upload(self, *, upload: MultipartUpload) -> None:
        self._require_path(upload.object_path)
        self._adapter.abort_multipart_upload(
            AdapterMultipartUpload(
                object_path=upload.object_path,
                upload_id=upload.upload_id,
            )
        )

    def _require_path(self, object_path: str) -> None:
        if object_path != self._object_path:
            raise ValueError("retrieval cache multipart object path changed")

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
        version_id=receipt.revision,
        etag=receipt.entity_token,
        bytes=receipt.stored_bytes,
        completed_at=receipt.completed_at,
    )


def _validate_integrity_parts(
    parts: tuple[MultipartPartReceipt, ...],
    *,
    expected_bytes: int,
) -> None:
    if not parts:
        return
    if (
        tuple(current.number for current in parts) != tuple(range(1, len(parts) + 1))
        or sum(current.bytes for current in parts) != expected_bytes
        or any(current.sha256 is None or len(current.sha256) != 64 for current in parts)
    ):
        raise ValueError("retrieval cache multipart integrity receipts are invalid")


__all__ = ["StorageAdapterRetrievalCache"]

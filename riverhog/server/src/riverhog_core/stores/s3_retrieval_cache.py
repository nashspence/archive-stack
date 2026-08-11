from __future__ import annotations

import hashlib
import os
import re
import time
from collections.abc import Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Any, cast

from time_formats import utc_timestamp_now

from riverhog_core.ports.retrieval_cache import RetrievalCacheReceipt
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.stores.s3_support import create_retrieval_cache_s3_client
from riverhog_core.throughput import (
    ArchiveThroughputTuning,
    ArchiveTransferResources,
    TransferTiming,
    log_transfer_timing,
)

_MIN_MULTIPART_BYTES = 5 * 1024 * 1024
_CONTENT_RANGE_RE = re.compile(r"bytes ([0-9]+)-([0-9]+)/([0-9]+|\\*)")


class S3RetrievalCache:
    def __init__(
        self,
        config: RuntimeConfig,
        *,
        throughput_tuning: ArchiveThroughputTuning | None = None,
        transfer_resources: ArchiveTransferResources | None = None,
    ) -> None:
        if config.retrieval_cache is None:
            raise ValueError("retrieval cache is not configured")
        self._config = config
        self._cache = config.retrieval_cache
        self._client = create_retrieval_cache_s3_client(config, self._cache)
        self._throughput = throughput_tuning or ArchiveThroughputTuning.from_env(os.environ)
        self._resources = transfer_resources or ArchiveTransferResources.from_tuning(
            self._throughput
        )

    def _object_path(self, source_store: str, collection_id: int, object_id: str) -> str:
        identity = f"{source_store}\0{collection_id}\0{object_id}".encode()
        digest = hashlib.sha256(identity).hexdigest()
        parts = [part for part in (self._cache.prefix, "objects", digest[:2], digest) if part]
        return "/".join(parts)

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
        object_path = self._object_path(source_store, collection_id, object_id)
        started = time.perf_counter()
        digest = hashlib.sha256()
        written = 0
        version_id: str | None = None
        queue_wait_seconds = 0.0
        source_seconds = 0.0
        integrity_seconds = 0.0
        remote_seconds = 0.0
        metadata = {
            "riverhog-cache-format": "encrypted-archive-object-v1",
            "riverhog-source-store": source_store,
            "riverhog-source-identity": hashlib.sha256(
                f"{collection_id}\0{object_id}".encode()
            ).hexdigest(),
        }

        if content_length < _MIN_MULTIPART_BYTES:
            small_body = bytearray()
            if content_length:
                queue_wait_seconds += self._resources.upload_bytes.acquire(content_length)
            try:
                with self._resources.retrieval_requests.reserve() as retrieval_wait:
                    queue_wait_seconds += retrieval_wait
                    chunks = iter(content)
                    while True:
                        source_started = time.perf_counter()
                        try:
                            chunk = next(chunks)
                        except StopIteration:
                            source_seconds += time.perf_counter() - source_started
                            break
                        source_seconds += time.perf_counter() - source_started
                        small_body.extend(chunk)
                        integrity_started = time.perf_counter()
                        digest.update(chunk)
                        integrity_seconds += time.perf_counter() - integrity_started
                        written += len(chunk)
                if written != content_length:
                    raise ValueError("retrieval cache stream length changed")
                with self._resources.upload_requests.reserve() as upload_wait:
                    queue_wait_seconds += upload_wait
                    remote_started = time.perf_counter()
                    response = cast(
                        dict[str, Any],
                        self._client.put_object(
                            Bucket=self._cache.bucket,
                            Key=object_path,
                            Body=bytes(small_body),
                            ContentLength=written,
                            Metadata=metadata,
                        ),
                    )
                    remote_seconds += time.perf_counter() - remote_started
            finally:
                if content_length:
                    self._resources.upload_bytes.release(content_length)
            version_id = str(response["VersionId"]) if response.get("VersionId") else None
        else:
            remote_started = time.perf_counter()
            created = cast(
                dict[str, Any],
                self._client.create_multipart_upload(
                    Bucket=self._cache.bucket,
                    Key=object_path,
                    Metadata=metadata,
                ),
            )
            remote_seconds += time.perf_counter() - remote_started
            upload_id = str(created["UploadId"])
            try:
                (
                    parts,
                    written,
                    part_queue_seconds,
                    part_source_seconds,
                    part_integrity_seconds,
                    part_remote_seconds,
                ) = self._upload_multipart_content(
                    object_path=object_path,
                    upload_id=upload_id,
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
                completed = cast(
                    dict[str, Any],
                    self._client.complete_multipart_upload(
                        Bucket=self._cache.bucket,
                        Key=object_path,
                        UploadId=upload_id,
                        MultipartUpload={"Parts": parts},
                    ),
                )
                remote_seconds += time.perf_counter() - remote_started
                version_id = str(completed["VersionId"]) if completed.get("VersionId") else None
            except Exception:
                self._client.abort_multipart_upload(
                    Bucket=self._cache.bucket,
                    Key=object_path,
                    UploadId=upload_id,
                )
                raise

        current = utc_timestamp_now()
        head_args: dict[str, object] = {"Bucket": self._cache.bucket, "Key": object_path}
        if version_id is not None:
            head_args["VersionId"] = version_id
        remote_started = time.perf_counter()
        head = cast(dict[str, Any], self._client.head_object(**head_args))
        remote_seconds += time.perf_counter() - remote_started
        if int(head.get("ContentLength", -1)) != content_length:
            raise RuntimeError("retrieval cache verification length mismatch")
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
        return RetrievalCacheReceipt(
            object_path=object_path,
            version_id=version_id,
            stored_bytes=written,
            stored_sha256=digest.hexdigest(),
            cached_at=current,
            verified_at=current,
        )

    def _upload_multipart_content(
        self,
        *,
        object_path: str,
        upload_id: str,
        content: Iterable[bytes],
        digest: Any,
    ) -> tuple[list[dict[str, object]], int, float, float, float, float]:
        part_bytes = self._config.archive_multipart_part_bytes
        worker_count = self._throughput.s3_part_concurrency
        window = worker_count * 2
        chunks = iter(content)
        buffer = bytearray()
        source_done = False
        written = 0
        next_part_number = 1
        pending: dict[Future[tuple[dict[str, object], float, float]], int] = {}
        completed: dict[int, dict[str, object]] = {}
        queue_wait_seconds = 0.0
        source_seconds = 0.0
        integrity_seconds = 0.0
        remote_seconds = 0.0

        def next_part() -> bytes | None:
            nonlocal integrity_seconds, source_done, source_seconds, written
            while len(buffer) < part_bytes and not source_done:
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
            if len(buffer) >= part_bytes:
                body = bytes(buffer[:part_bytes])
                del buffer[:part_bytes]
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
                        reserved = len(body)
                        queue_wait_seconds += self._resources.upload_bytes.acquire(reserved)
                        part_number = next_part_number
                        next_part_number += 1
                        try:
                            future = executor.submit(
                                self._upload_part,
                                object_path=object_path,
                                upload_id=upload_id,
                                part_number=part_number,
                                body=body,
                            )
                        except BaseException:
                            self._resources.upload_bytes.release(reserved)
                            raise

                        def release_buffer(
                            _future: Future[tuple[dict[str, object], float, float]],
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
            [completed[number] for number in sorted(completed)],
            written,
            queue_wait_seconds,
            source_seconds,
            integrity_seconds,
            remote_seconds,
        )

    def _upload_part(
        self,
        *,
        object_path: str,
        upload_id: str,
        part_number: int,
        body: bytes,
    ) -> tuple[dict[str, object], float, float]:
        with self._resources.upload_requests.reserve() as upload_wait:
            remote_started = time.perf_counter()
            response = self._client.upload_part(
                Bucket=self._cache.bucket,
                Key=object_path,
                UploadId=upload_id,
                PartNumber=part_number,
                Body=body,
                ContentLength=len(body),
            )
            remote_seconds = time.perf_counter() - remote_started
        return (
            {"PartNumber": part_number, "ETag": str(response["ETag"])},
            upload_wait,
            remote_seconds,
        )

    def iter_object(
        self,
        *,
        object_path: str,
        version_id: str | None,
        expected_bytes: int,
        expected_sha256: str,
    ) -> Iterator[bytes]:
        request: dict[str, object] = {"Bucket": self._cache.bucket, "Key": object_path}
        if version_id is not None:
            request["VersionId"] = version_id
        response = self._client.get_object(**request)
        body = response["Body"]
        digest = hashlib.sha256()
        size = 0
        try:
            for chunk in body.iter_chunks(chunk_size=1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
                yield chunk
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()
        if size != expected_bytes or digest.hexdigest() != expected_sha256:
            raise RuntimeError("retrieval cache object does not match its verified record")

    def iter_object_range(
        self,
        *,
        object_path: str,
        version_id: str | None,
        offset: int,
        size: int,
    ) -> Iterator[bytes]:
        if offset < 0 or size < 0:
            raise ValueError("retrieval cache range must be non-negative")
        if size == 0:
            return
        end = offset + size - 1
        request: dict[str, object] = {
            "Bucket": self._cache.bucket,
            "Key": object_path,
            "Range": f"bytes={offset}-{end}",
        }
        if version_id is not None:
            request["VersionId"] = version_id
        response = self._client.get_object(**request)
        if int(str(response.get("ContentLength", -1))) != size:
            raise RuntimeError("retrieval cache range length mismatch")
        match = _CONTENT_RANGE_RE.fullmatch(str(response.get("ContentRange", "")))
        if match is None or int(match.group(1)) != offset or int(match.group(2)) != end:
            raise RuntimeError("retrieval cache response range mismatch")
        body = response["Body"]
        emitted = 0
        try:
            for chunk in body.iter_chunks(chunk_size=1024 * 1024):
                emitted += len(chunk)
                if emitted > size:
                    raise RuntimeError("retrieval cache range contains trailing bytes")
                yield chunk
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()
        if emitted != size:
            raise RuntimeError("retrieval cache range ended before its declared length")

    def delete(self, *, object_path: str, version_id: str | None) -> None:
        request: dict[str, object] = {"Bucket": self._cache.bucket, "Key": object_path}
        if version_id is not None:
            request["VersionId"] = version_id
        self._client.delete_object(**request)

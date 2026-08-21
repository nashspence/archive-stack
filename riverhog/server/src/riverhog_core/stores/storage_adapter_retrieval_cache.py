from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from datetime import datetime

from time_formats import format_utc_timestamp

from riverhog_core.domain.retrieval_cache import RetrievalCacheReceipt
from riverhog_core.ports.archive_objects import (
    CompletedObjectReceipt,
    MultipartPartReceipt,
    MultipartUpload,
)
from riverhog_core.stores.storage_adapter_object_store import (
    StorageAdapterObjectStore,
    StorageAdapterRuntime,
)


class StorageAdapterRetrievalCache:
    """Riverhog cache policy over one immediate-read storage adapter."""

    def __init__(
        self,
        runtime: StorageAdapterRuntime,
        *,
        multipart_part_bytes: int,
    ) -> None:
        if runtime.registration.expected_profile_read_mode != "immediate":
            raise ValueError("retrieval cache requires an immediate-read storage profile")
        if multipart_part_bytes < 1:
            raise ValueError("retrieval-cache multipart size must be positive")
        self.runtime = runtime
        self._objects = StorageAdapterObjectStore(runtime)
        self._part_bytes = multipart_part_bytes

    @staticmethod
    def _object_path(source_store: str, collection_id: int, object_id: str) -> str:
        identity = f"{source_store}\0{collection_id}\0{object_id}".encode()
        digest = hashlib.sha256(identity).hexdigest()
        return f"cache/objects/{digest[:2]}/{digest}"

    def abort_incomplete_multipart_uploads(
        self,
        *,
        initiated_before: datetime,
    ) -> int:
        if initiated_before.tzinfo is None:
            raise ValueError("retrieval cache multipart cutoff must be timezone-aware")
        result = self.runtime.client.abort_incomplete_uploads(
            initiated_before=format_utc_timestamp(initiated_before)
        )
        return result.affected

    def multipart_object_store(
        self,
        *,
        source_store: str,
        collection_id: int,
        object_id: str,
    ) -> _CacheMultipartObjectStore:
        return _CacheMultipartObjectStore(
            objects=self._objects,
            cache_path=self._object_path(source_store, collection_id, object_id),
            identity={
                "riverhog-cache-format": "encrypted-archive-object-v1",
                "riverhog-source-store": source_store,
                "riverhog-source-identity": hashlib.sha256(
                    f"{collection_id}\0{object_id}".encode()
                ).hexdigest(),
            },
        )

    def verify_multipart_object(
        self,
        *,
        completed: CompletedObjectReceipt,
        parts: tuple[MultipartPartReceipt, ...] = (),
    ) -> RetrievalCacheReceipt:
        expected = tuple(sorted(parts, key=lambda current: current.number))
        if expected and (
            tuple(current.number for current in expected) != tuple(range(1, len(expected) + 1))
            or sum(current.stored_bytes for current in expected) != completed.stored_bytes
        ):
            raise ValueError("retrieval cache multipart receipts are invalid")
        digest = hashlib.sha256()
        received = 0
        for chunk in self._objects.iter_object(
            object_path=completed.object_path,
            revision=completed.revision,
        ):
            digest.update(chunk)
            received += len(chunk)
        if received != completed.stored_bytes or digest.hexdigest() != completed.stored_sha256:
            raise RuntimeError("retrieval cache object differs from its adapter receipt")
        verified_at = format_utc_timestamp(datetime.now().astimezone())
        return RetrievalCacheReceipt(
            object_path=completed.object_path,
            revision=completed.revision,
            stored_bytes=completed.stored_bytes,
            stored_sha256=completed.stored_sha256,
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
        descriptor = self.runtime.refresh_descriptor()
        writer = self.multipart_object_store(
            source_store=source_store,
            collection_id=collection_id,
            object_id=object_id,
        )
        if (
            self._part_bytes < descriptor.minimum_nonfinal_part_bytes
            or self._part_bytes > descriptor.maximum_part_bytes
        ):
            raise ValueError("retrieval-cache multipart size is outside the adapter runtime limits")
        upload = writer.create_multipart_upload(
            object_path="cache-source-identity",
            content_type="application/octet-stream",
            metadata={},
            expected_bytes=content_length,
        )
        digest = hashlib.sha256()
        buffer = bytearray()
        receipts: list[MultipartPartReceipt] = []
        received = 0
        for chunk in content:
            data = bytes(chunk)
            digest.update(data)
            received += len(data)
            buffer.extend(data)
            while len(buffer) >= self._part_bytes:
                part = bytes(buffer[: self._part_bytes])
                del buffer[: self._part_bytes]
                receipts.append(
                    writer.upload_part(
                        upload=upload,
                        number=len(receipts) + 1,
                        content=part,
                    )
                )
        if buffer:
            receipts.append(
                writer.upload_part(
                    upload=upload,
                    number=len(receipts) + 1,
                    content=bytes(buffer),
                )
            )
        if received != content_length:
            writer.abort_multipart_upload(upload=upload)
            raise ValueError("retrieval cache stream length changed")
        completed = writer.complete_multipart_upload(
            upload=upload,
            parts=tuple(receipts),
            expected_bytes=received,
            expected_metadata={},
        )
        if completed.stored_sha256 != digest.hexdigest():
            raise RuntimeError("retrieval cache adapter receipt differs from the source")
        return self.verify_multipart_object(completed=completed, parts=tuple(receipts))

    def iter_object(
        self,
        *,
        object_path: str,
        revision: str,
        expected_bytes: int,
        expected_sha256: str,
    ) -> Iterator[bytes]:
        digest = hashlib.sha256()
        emitted = 0
        for chunk in self._objects.iter_object(
            object_path=object_path,
            revision=revision,
        ):
            digest.update(chunk)
            emitted += len(chunk)
            yield chunk
        if emitted != expected_bytes or digest.hexdigest() != expected_sha256:
            raise RuntimeError("retrieval cache object does not match its verified record")

    def iter_object_range(
        self,
        *,
        object_path: str,
        revision: str,
        offset: int,
        size: int,
    ) -> Iterator[bytes]:
        yield from self._objects.iter_object_range(
            object_path=object_path,
            revision=revision,
            offset=offset,
            size=size,
        )

    def delete(self, *, object_path: str, revision: str) -> None:
        self._objects.delete_object(object_path=object_path, revision=revision)


class _CacheMultipartObjectStore:
    def __init__(
        self,
        *,
        objects: StorageAdapterObjectStore,
        cache_path: str,
        identity: dict[str, str],
    ) -> None:
        self._objects = objects
        self._cache_path = cache_path
        self._identity = identity

    def _metadata(self, supplied: dict[str, str]) -> dict[str, str]:
        return {**supplied, **self._identity}

    def create_multipart_upload(
        self,
        *,
        object_path: str,
        content_type: str,
        metadata: dict[str, str],
        expected_bytes: int,
    ) -> MultipartUpload:
        _ = object_path
        return self._objects.create_multipart_upload(
            object_path=self._cache_path,
            content_type=content_type,
            metadata=self._metadata(metadata),
            expected_bytes=expected_bytes,
        )

    def upload_part(
        self,
        *,
        upload: MultipartUpload,
        number: int,
        content: bytes,
    ) -> MultipartPartReceipt:
        return self._objects.upload_part(upload=upload, number=number, content=content)

    def list_parts(self, *, upload: MultipartUpload) -> tuple[MultipartPartReceipt, ...]:
        return self._objects.list_parts(upload=upload)

    def complete_multipart_upload(
        self,
        *,
        upload: MultipartUpload,
        parts: tuple[MultipartPartReceipt, ...],
        expected_bytes: int,
        expected_metadata: dict[str, str],
    ) -> CompletedObjectReceipt:
        return self._objects.complete_multipart_upload(
            upload=upload,
            parts=parts,
            expected_bytes=expected_bytes,
            expected_metadata=self._metadata(expected_metadata),
        )

    def head_completed_object(
        self,
        *,
        object_path: str,
        expected_metadata: dict[str, str],
    ) -> CompletedObjectReceipt | None:
        _ = object_path
        return self._objects.head_completed_object(
            object_path=self._cache_path,
            expected_metadata=self._metadata(expected_metadata),
        )

    def abort_multipart_upload(self, *, upload: MultipartUpload) -> None:
        self._objects.abort_multipart_upload(upload=upload)


__all__ = ["StorageAdapterRetrievalCache"]

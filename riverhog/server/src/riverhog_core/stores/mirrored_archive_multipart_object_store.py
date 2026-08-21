from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from riverhog_core.ports.archive_objects import (
    ArchiveMultipartObjectStore,
    CompletedObjectReceipt,
    MultipartPartReceipt,
    MultipartUpload,
)
from riverhog_core.ports.retrieval_cache import RetrievalCache

_UPLOAD_SCHEMA = "archive-cache-mirror-upload/v1"


class MirroredArchiveMultipartObjectStore:
    """Write identical encrypted parts to archive authority and retrieval cache."""

    def __init__(
        self,
        *,
        archive: ArchiveMultipartObjectStore,
        cache: RetrievalCache,
        source_store: str,
        collection_id: int,
        object_id: str,
    ) -> None:
        self._archive = archive
        self._cache = cache
        self._cache_objects = cache.multipart_object_store(
            source_store=source_store,
            collection_id=collection_id,
            object_id=object_id,
        )

    def create_multipart_upload(
        self,
        *,
        object_path: str,
        content_type: str,
        metadata: dict[str, str],
        expected_bytes: int,
    ) -> MultipartUpload:
        archive_upload = self._archive.create_multipart_upload(
            object_path=object_path,
            content_type=content_type,
            metadata=metadata,
            expected_bytes=expected_bytes,
        )
        try:
            cache_completed = self._cache_objects.head_completed_object(
                object_path=object_path,
                expected_metadata=metadata,
            )
            cache_upload = (
                None
                if cache_completed is not None
                else self._cache_objects.create_multipart_upload(
                    object_path=object_path,
                    content_type=content_type,
                    metadata=metadata,
                    expected_bytes=expected_bytes,
                )
            )
        except BaseException:
            self._archive.abort_multipart_upload(upload=archive_upload)
            raise
        return MultipartUpload(
            object_path=object_path,
            transfer_id=_encode_transfer_id(
                archive_upload=archive_upload,
                cache_upload=cache_upload,
            ),
        )

    def upload_part(
        self,
        *,
        upload: MultipartUpload,
        number: int,
        content: bytes,
    ) -> MultipartPartReceipt:
        archive_upload, cache_upload = _decode_transfer_id(upload)
        if cache_upload is None:
            return self._archive.upload_part(
                upload=archive_upload,
                number=number,
                content=content,
            )
        if cache_upload is None:
            raise RuntimeError("retrieval cache mirror has no upload or completed object")
        with ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="riverhog-archive-cache-mirror",
        ) as executor:
            archive_future = executor.submit(
                self._archive.upload_part,
                upload=archive_upload,
                number=number,
                content=content,
            )
            cache_future = executor.submit(
                self._cache_objects.upload_part,
                upload=cache_upload,
                number=number,
                content=content,
            )
            archive_receipt = archive_future.result()
            cache_receipt = cache_future.result()
        if (
            archive_receipt.number != cache_receipt.number
            or archive_receipt.stored_bytes != cache_receipt.stored_bytes
        ):
            raise RuntimeError("archive and retrieval cache multipart receipts disagree")
        return archive_receipt

    def list_parts(self, *, upload: MultipartUpload) -> tuple[MultipartPartReceipt, ...]:
        archive_upload, cache_upload = _decode_transfer_id(upload)
        archive_parts = self._archive.list_parts(upload=archive_upload)
        if cache_upload is None:
            return archive_parts
        if cache_upload is None:
            raise RuntimeError("retrieval cache mirror has no upload or completed object")
        cache_parts = {
            current.number: current
            for current in self._cache_objects.list_parts(upload=cache_upload)
        }
        return tuple(
            current
            for current in archive_parts
            if (mirrored := cache_parts.get(current.number)) is not None
            and mirrored.stored_bytes == current.stored_bytes
            and mirrored.stored_sha256 == current.stored_sha256
        )

    def complete_multipart_upload(
        self,
        *,
        upload: MultipartUpload,
        parts: tuple[MultipartPartReceipt, ...],
        expected_bytes: int,
        expected_metadata: dict[str, str],
    ) -> CompletedObjectReceipt:
        archive_upload, cache_upload = _decode_transfer_id(upload)
        cache_completed = self._cache_objects.head_completed_object(
            object_path=upload.object_path,
            expected_metadata=expected_metadata,
        )
        if cache_completed is None:
            if cache_upload is None:
                raise RuntimeError("retrieval cache mirror has no multipart upload")
            cache_parts = self._cache_objects.list_parts(upload=cache_upload)
            _require_matching_parts(parts, cache_parts)
            cache_completed = self._cache_objects.complete_multipart_upload(
                upload=cache_upload,
                parts=cache_parts,
                expected_bytes=expected_bytes,
                expected_metadata=expected_metadata,
            )
        if cache_completed.stored_bytes != expected_bytes:
            raise RuntimeError("retrieval cache mirror byte count differs from archive upload")
        cache_receipt = self._cache.verify_multipart_object(
            completed=cache_completed,
            parts=parts,
        )
        archive_completed = self._archive.complete_multipart_upload(
            upload=archive_upload,
            parts=parts,
            expected_bytes=expected_bytes,
            expected_metadata=expected_metadata,
        )
        return replace(archive_completed, retrieval_cache=cache_receipt)

    def head_completed_object(
        self,
        *,
        object_path: str,
        expected_metadata: dict[str, str],
    ) -> CompletedObjectReceipt | None:
        archive_completed = self._archive.head_completed_object(
            object_path=object_path,
            expected_metadata=expected_metadata,
        )
        if archive_completed is None:
            return None
        cache_completed = self._cache_objects.head_completed_object(
            object_path=object_path,
            expected_metadata=expected_metadata,
        )
        if (
            cache_completed is None
            or cache_completed.stored_bytes != archive_completed.stored_bytes
        ):
            raise RuntimeError("completed archive object is missing its required retrieval cache")
        return replace(
            archive_completed,
            retrieval_cache=self._cache.verify_multipart_object(completed=cache_completed),
        )

    def abort_multipart_upload(self, *, upload: MultipartUpload) -> None:
        archive_upload, cache_upload = _decode_transfer_id(upload)
        archive_completed = self._archive.head_completed_object(
            object_path=upload.object_path,
            expected_metadata={},
        )
        cache_completed = self._cache_objects.head_completed_object(
            object_path=upload.object_path,
            expected_metadata={},
        )
        archive_error: BaseException | None = None
        try:
            self._archive.abort_multipart_upload(upload=archive_upload)
        except BaseException as exc:
            archive_error = exc
        if cache_upload is not None and cache_completed is None:
            self._cache_objects.abort_multipart_upload(upload=cache_upload)
        if cache_completed is not None and archive_completed is None:
            self._cache.delete(
                object_path=cache_completed.object_path,
                revision=cache_completed.revision,
            )
        if archive_error is not None:
            raise archive_error


def _encode_transfer_id(
    *,
    archive_upload: MultipartUpload,
    cache_upload: MultipartUpload | None,
) -> str:
    return json.dumps(
        {
            "schema": _UPLOAD_SCHEMA,
            "archive": {
                "object_path": archive_upload.object_path,
                "transfer_id": archive_upload.transfer_id,
            },
            "cache": (
                {
                    "object_path": cache_upload.object_path,
                    "transfer_id": cache_upload.transfer_id,
                }
                if cache_upload is not None
                else None
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_transfer_id(upload: MultipartUpload) -> tuple[MultipartUpload, MultipartUpload | None]:
    try:
        payload = json.loads(upload.transfer_id)
    except json.JSONDecodeError as exc:
        raise ValueError("archive-cache mirror upload id is invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema") != _UPLOAD_SCHEMA:
        raise ValueError("archive-cache mirror upload id schema mismatch")
    archive = _upload_from_payload(payload.get("archive"), label="archive")
    cache_payload = payload.get("cache")
    cache = None if cache_payload is None else _upload_from_payload(cache_payload, label="cache")
    if archive.object_path != upload.object_path:
        raise ValueError("archive-cache mirror object path changed")
    return archive, cache


def _upload_from_payload(value: object, *, label: str) -> MultipartUpload:
    if not isinstance(value, dict):
        raise ValueError(f"archive-cache mirror {label} upload is invalid")
    object_path = str(value.get("object_path", ""))
    transfer_id = str(value.get("transfer_id", ""))
    if not object_path or not transfer_id:
        raise ValueError(f"archive-cache mirror {label} upload identity is invalid")
    return MultipartUpload(object_path, transfer_id)


def _require_matching_parts(
    archive: tuple[MultipartPartReceipt, ...],
    cache: tuple[MultipartPartReceipt, ...],
) -> None:
    archive_shape = tuple(
        (current.number, current.stored_bytes, current.stored_sha256)
        for current in archive
    )
    cache_shape = tuple(
        (current.number, current.stored_bytes, current.stored_sha256)
        for current in cache
    )
    if archive_shape != cache_shape:
        raise RuntimeError("retrieval cache multipart parts do not match the archive upload")

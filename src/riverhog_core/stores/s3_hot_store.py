from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable, Iterator
from typing import Any, cast

from riverhog_core.domain.errors import NotFound
from riverhog_core.ports.archive_store import (
    ArchiveMultipartUploadedPart,
    ArchiveMultipartUploadState,
    ArchiveMultipartUploadTracker,
)
from riverhog_core.ports.hot_store import HotCollectionFile, HotCollectionListing, HotFileStat
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.stores.s3_support import create_s3_client

_MIN_MULTIPART_PART_SIZE = 5 * 1024 * 1024
_MAX_MULTIPART_PART_SIZE = 5 * 1024 * 1024 * 1024
_MAX_MULTIPART_PARTS = 10_000
_FILE_BYTES_METADATA = "riverhog-file-bytes"
_FILE_SHA256_METADATA = "riverhog-file-sha256"
_LOG = logging.getLogger(__name__)


def _multipart_part_size(content_length: int) -> int:
    part_size = max(
        _MIN_MULTIPART_PART_SIZE,
        (content_length + _MAX_MULTIPART_PARTS - 1) // _MAX_MULTIPART_PARTS,
    )
    if part_size > _MAX_MULTIPART_PART_SIZE:
        raise ValueError("collection file stream exceeds S3 multipart object size limit")
    return part_size


def _should_log_multipart_progress(part_number: int, expected_part_count: int) -> bool:
    return (
        part_number == 1
        or part_number == expected_part_count
        or expected_part_count <= 20
        or part_number % 100 == 0
    )


def _iter_chunks_after_skipping(chunks: Iterable[bytes], skip_bytes: int) -> Iterator[bytes]:
    remaining = skip_bytes
    for chunk in chunks:
        if remaining <= 0:
            yield chunk
            continue
        if len(chunk) <= remaining:
            remaining -= len(chunk)
            continue
        yield chunk[remaining:]
        remaining = 0
    if remaining:
        raise ValueError("collection hot file stream ended before resumable upload offset")


def _validate_recorded_parts_exist_remotely(
    recorded_parts: list[ArchiveMultipartUploadedPart],
    remote_parts: list[dict[str, object]],
) -> None:
    remote_parts_by_number = {int(str(part["PartNumber"])): part for part in remote_parts}
    for part in recorded_parts:
        remote = remote_parts_by_number.get(part.part_number)
        if remote is None:
            raise ValueError("collection hot file multipart upload is missing a recorded part")
        if str(remote["ETag"]) != part.etag or int(str(remote["Size"])) != part.size:
            raise ValueError("collection hot file multipart upload remote part mismatch")


class S3HotStore:
    def __init__(self, config: RuntimeConfig) -> None:
        self._bucket = config.s3_bucket
        self._client = create_s3_client(config)
        self._single_put_max_bytes = config.hot_single_put_max_bytes

    def _key(self, collection_id: str, path: str) -> str:
        return f"collections/{collection_id}/{path}"

    def put_collection_file(self, collection_id: str, path: str, content: bytes) -> None:
        sha256 = hashlib.sha256(content).hexdigest()
        self._client.put_object(
            Bucket=self._bucket,
            Key=self._key(collection_id, path),
            Body=content,
            ContentLength=len(content),
            Metadata=_file_metadata(content_length=len(content), sha256=sha256),
        )

    def put_collection_file_stream(
        self,
        collection_id: str,
        path: str,
        chunks: Iterable[bytes],
        *,
        content_length: int,
        sha256: str | None = None,
    ) -> None:
        self.put_collection_file_stream_resumable(
            collection_id,
            path,
            chunks,
            content_length=content_length,
            sha256=sha256,
            multipart_tracker=None,
        )

    def put_collection_file_stream_resumable(
        self,
        collection_id: str,
        path: str,
        chunks: Iterable[bytes],
        *,
        content_length: int,
        sha256: str | None = None,
        multipart_tracker: ArchiveMultipartUploadTracker | None = None,
    ) -> None:
        final_key = self._key(collection_id, path)
        metadata = _file_metadata(content_length=content_length, sha256=sha256)
        if content_length <= self._single_put_max_bytes:
            body = b"".join(chunks)
            if len(body) != content_length:
                raise ValueError(f"collection file stream byte count mismatch: {path}")
            self._client.put_object(
                Bucket=self._bucket,
                Key=final_key,
                Body=body,
                ContentLength=content_length,
                Metadata=metadata,
            )
            return

        upload_id: str | None = None
        upload_state: ArchiveMultipartUploadState | None = None
        part_number = 1
        completed_parts: list[ArchiveMultipartUploadedPart] = []
        buffer = bytearray()
        part_size = _multipart_part_size(content_length)
        expected_part_count = (content_length + part_size - 1) // part_size
        uploaded_bytes = 0
        resumed_part_count = 0
        skip_bytes = 0
        size = 0

        if multipart_tracker is not None and sha256 is not None:
            upload_state = multipart_tracker.load_multipart_upload(
                collection_id=collection_id,
                object_path=final_key,
                part_size=part_size,
                content_length=content_length,
                sha256=sha256,
            )
            if upload_state is not None:
                upload_id = upload_state.upload_id
                try:
                    resumed_parts = self._contiguous_uploaded_parts(
                        object_key=final_key,
                        upload_id=upload_state.upload_id,
                        recorded_parts=upload_state.parts,
                        part_size=part_size,
                        content_length=content_length,
                    )
                except Exception as exc:
                    if not _is_missing_upload_error(exc):
                        raise
                    multipart_tracker.clear_multipart_upload(
                        collection_id=collection_id,
                        upload_id=upload_state.upload_id,
                    )
                    upload_id = None
                    upload_state = None
                    resumed_parts = []
                if upload_state is not None:
                    resumed_part_count = len(resumed_parts)
                    skip_bytes = sum(part.size for part in resumed_parts)
                    part_number = resumed_part_count + 1
                    uploaded_bytes = skip_bytes
                    completed_parts.extend(resumed_parts)
                    _LOG.info(
                        "resuming hot-store multipart upload for %s: upload_id=%s "
                        "parts=%s/%s bytes=%s/%s",
                        final_key,
                        upload_state.upload_id,
                        resumed_part_count,
                        expected_part_count,
                        uploaded_bytes,
                        content_length,
                    )

        def ensure_upload() -> str:
            nonlocal upload_id, upload_state
            if upload_id is None:
                _LOG.info(
                    "starting hot-store multipart upload for %s: size=%s part_size=%s parts=%s",
                    final_key,
                    content_length,
                    part_size,
                    expected_part_count,
                )
                response = cast(
                    dict[str, Any],
                    self._client.create_multipart_upload(
                        Bucket=self._bucket,
                        Key=final_key,
                        Metadata=metadata,
                    ),
                )
                upload_id = str(response["UploadId"])
                upload_state = ArchiveMultipartUploadState(
                    upload_id=upload_id,
                    object_path=final_key,
                    part_size=part_size,
                    content_length=content_length,
                    sha256=sha256 or "",
                )
                if multipart_tracker is not None and sha256 is not None:
                    multipart_tracker.save_multipart_upload(
                        collection_id=collection_id,
                        state=upload_state,
                    )
            return upload_id

        def upload_part(body: bytes) -> None:
            nonlocal part_number, uploaded_bytes
            current_part_number = part_number
            response = cast(
                dict[str, Any],
                self._client.upload_part(
                    Bucket=self._bucket,
                    Key=final_key,
                    UploadId=ensure_upload(),
                    PartNumber=current_part_number,
                    Body=body,
                ),
            )
            part = ArchiveMultipartUploadedPart(
                part_number=current_part_number,
                etag=str(response["ETag"]),
                size=len(body),
            )
            completed_parts.append(part)
            uploaded_bytes += len(body)
            if multipart_tracker is not None and upload_state is not None:
                multipart_tracker.record_multipart_upload_progress(
                    collection_id=collection_id,
                    state=upload_state,
                    part=part,
                    uploaded_bytes=uploaded_bytes,
                    uploaded_parts=current_part_number,
                    total_parts=expected_part_count,
                )
            if _should_log_multipart_progress(current_part_number, expected_part_count):
                _LOG.info(
                    "hot-store multipart upload progress for %s: part=%s/%s bytes=%s/%s pct=%.2f",
                    final_key,
                    current_part_number,
                    expected_part_count,
                    uploaded_bytes,
                    content_length,
                    (uploaded_bytes / content_length * 100.0) if content_length else 100.0,
                )
            part_number += 1

        try:
            for chunk in _iter_chunks_after_skipping(chunks, skip_bytes):
                size += len(chunk)
                chunk_view = memoryview(chunk)
                offset = 0
                while offset < len(chunk_view):
                    bytes_to_copy = min(
                        part_size - len(buffer),
                        len(chunk_view) - offset,
                    )
                    buffer.extend(chunk_view[offset : offset + bytes_to_copy])
                    offset += bytes_to_copy
                    if len(buffer) == part_size:
                        upload_part(bytes(buffer))
                        buffer.clear()

            if size + skip_bytes != content_length:
                raise ValueError(f"collection file stream byte count mismatch: {path}")
            if buffer:
                upload_part(bytes(buffer))
                buffer.clear()
            if upload_id is None:
                self._client.put_object(
                    Bucket=self._bucket,
                    Key=final_key,
                    Body=b"",
                    ContentLength=0,
                    Metadata=metadata,
                )
                return
            remote_parts = self._list_uploaded_parts(object_key=final_key, upload_id=upload_id)
            if len(completed_parts) != expected_part_count:
                raise ValueError(
                    "collection hot file multipart upload is missing parts before completion"
                )
            _validate_recorded_parts_exist_remotely(completed_parts, remote_parts)
            self._client.complete_multipart_upload(
                Bucket=self._bucket,
                Key=final_key,
                UploadId=upload_id,
                MultipartUpload={
                    "Parts": [
                        {"PartNumber": part.part_number, "ETag": part.etag}
                        for part in completed_parts
                    ]
                },
            )
            if multipart_tracker is not None:
                multipart_tracker.clear_multipart_upload(
                    collection_id=collection_id,
                    upload_id=upload_id,
                )
        except Exception as exc:
            if upload_id is not None and multipart_tracker is None:
                try:
                    self._client.abort_multipart_upload(
                        Bucket=self._bucket,
                        Key=final_key,
                        UploadId=upload_id,
                    )
                except Exception as cleanup_exc:
                    exc.add_note(
                        f"failed to abort S3 multipart upload {upload_id}: {cleanup_exc!r}"
                    )
            elif upload_id is not None:
                _LOG.warning(
                    "leaving incomplete hot-store multipart upload for %s resumable: "
                    "upload_id=%s uploaded_bytes=%s/%s resumed_parts=%s",
                    final_key,
                    upload_id,
                    uploaded_bytes,
                    content_length,
                    resumed_part_count,
                    exc_info=True,
                )
            raise

    def _list_uploaded_parts(
        self,
        *,
        object_key: str,
        upload_id: str,
    ) -> list[dict[str, object]]:
        parts: list[dict[str, object]] = []
        marker = 0
        while True:
            request: dict[str, object] = {
                "Bucket": self._bucket,
                "Key": object_key,
                "UploadId": upload_id,
            }
            if marker:
                request["PartNumberMarker"] = marker
            response = cast(dict[str, Any], self._client.list_parts(**request))
            for part in response.get("Parts", []):
                if not isinstance(part, dict):
                    continue
                part_number = int(part["PartNumber"])
                parts.append(
                    {
                        "PartNumber": part_number,
                        "ETag": str(part["ETag"]),
                        "Size": int(part.get("Size", 0)),
                    }
                )
                marker = part_number
            if not response.get("IsTruncated"):
                return sorted(parts, key=lambda current: int(str(current["PartNumber"])))
            marker = int(str(response.get("NextPartNumberMarker", marker)))

    def _contiguous_uploaded_parts(
        self,
        *,
        object_key: str,
        upload_id: str,
        recorded_parts: tuple[ArchiveMultipartUploadedPart, ...],
        part_size: int,
        content_length: int,
    ) -> list[ArchiveMultipartUploadedPart]:
        expected_part_count = (content_length + part_size - 1) // part_size
        remote_parts_by_number = {
            int(str(part["PartNumber"])): part
            for part in self._list_uploaded_parts(object_key=object_key, upload_id=upload_id)
        }
        recorded_parts_by_number = {part.part_number: part for part in recorded_parts}
        contiguous: list[ArchiveMultipartUploadedPart] = []
        for part_number in range(1, expected_part_count + 1):
            recorded = recorded_parts_by_number.get(part_number)
            remote = remote_parts_by_number.get(part_number)
            if recorded is None or remote is None:
                break
            expected_size = (
                content_length - part_size * (expected_part_count - 1)
                if part_number == expected_part_count
                else part_size
            )
            if recorded.size != expected_size:
                break
            if str(remote["ETag"]) != recorded.etag or int(str(remote["Size"])) != recorded.size:
                break
            contiguous.append(recorded)
        return contiguous

    def get_collection_file(self, collection_id: str, path: str) -> bytes:
        return b"".join(self.iter_collection_file(collection_id, path))

    def iter_collection_file(
        self,
        collection_id: str,
        path: str,
        *,
        offset: int = 0,
        size: int | None = None,
    ) -> Iterator[bytes]:
        if offset < 0:
            raise ValueError("offset must be >= 0")
        if size is not None and size < 0:
            raise ValueError("size must be >= 0")
        if size == 0:
            return

        request: dict[str, object] = {
            "Bucket": self._bucket,
            "Key": self._key(collection_id, path),
        }
        if offset or size is not None:
            if size is None:
                request["Range"] = f"bytes={offset}-"
            else:
                request["Range"] = f"bytes={offset}-{offset + size - 1}"

        try:
            response = self._client.get_object(**request)
        except self._client.exceptions.ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
                raise
            raise NotFound(f"file not found in hot store: {collection_id}/{path}") from exc
        body = response["Body"]
        try:
            iter_chunks = getattr(body, "iter_chunks", None)
            if callable(iter_chunks):
                yield from iter_chunks(chunk_size=1024 * 1024)
            else:
                yield cast(bytes, body.read())
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()

    def stat_collection_file(self, collection_id: str, path: str) -> HotFileStat | None:
        try:
            head = self._client.head_object(Bucket=self._bucket, Key=self._key(collection_id, path))
        except self._client.exceptions.ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return None
            raise
        metadata = cast(dict[str, str], head.get("Metadata") or {})
        sha256 = metadata.get(_FILE_SHA256_METADATA)
        return HotFileStat(bytes=int(head.get("ContentLength", 0)), sha256=sha256 or None)

    def has_collection_file(self, collection_id: str, path: str) -> bool:
        return self.stat_collection_file(collection_id, path) is not None

    def delete_collection_file(self, collection_id: str, path: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=self._key(collection_id, path))

    def abort_collection_file_multipart_upload(
        self,
        collection_id: str,
        path: str,
        upload_id: str,
    ) -> None:
        try:
            self._client.abort_multipart_upload(
                Bucket=self._bucket,
                Key=self._key(collection_id, path),
                UploadId=upload_id,
            )
        except self._client.exceptions.ClientError as exc:
            if not _is_missing_upload_error(exc):
                raise

    def list_collection_files(self, collection_id: str) -> HotCollectionListing:
        paginator = self._client.get_paginator("list_objects_v2")
        prefix = f"collections/{collection_id}/"
        files: list[HotCollectionFile] = []
        file_count = 0
        total_bytes = 0
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for entry in page.get("Contents", []):
                key = str(entry["Key"])
                if key.endswith(".info") or key.endswith(".part"):
                    continue
                size = int(entry.get("Size", 0))
                files.append(HotCollectionFile(path=key.removeprefix(prefix), bytes=size))
                file_count += 1
                total_bytes += size
        return HotCollectionListing(
            files=tuple(sorted(files, key=lambda file: file.path)),
            file_count=file_count,
            total_bytes=total_bytes,
        )


def _file_metadata(*, content_length: int, sha256: str | None) -> dict[str, str]:
    metadata = {_FILE_BYTES_METADATA: str(content_length)}
    if sha256:
        metadata[_FILE_SHA256_METADATA] = sha256
    return metadata


def _is_missing_upload_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error", {})
    if not isinstance(error, dict):
        return False
    code = str(error.get("Code", "")).strip()
    return code in {"NoSuchUpload", "404", "NotFound"}

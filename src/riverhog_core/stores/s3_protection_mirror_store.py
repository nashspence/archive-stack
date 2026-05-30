from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from typing import Any, cast

from riverhog_core.domain.errors import NotFound
from riverhog_core.ports.archive_store import (
    ArchiveMultipartUploadedPart,
    ArchiveMultipartUploadState,
    ArchiveMultipartUploadTracker,
)
from riverhog_core.ports.protection_mirror import ProtectionMirrorArchiveStat
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.stores.s3_hot_store import (
    _is_missing_upload_error,
    _iter_chunks_after_skipping,
    _multipart_part_size,
    _should_log_multipart_progress,
    _validate_recorded_parts_exist_remotely,
)
from riverhog_core.stores.s3_support import create_protection_mirror_s3_client

_ARCHIVE_BYTES_METADATA = "riverhog-archive-bytes"
_ARCHIVE_SHA256_METADATA = "riverhog-archive-sha256"
_LOG = logging.getLogger(__name__)


class S3ProtectionMirrorStore:
    def __init__(self, config: RuntimeConfig) -> None:
        if not config.protection_mirror_enabled:
            raise RuntimeError("protection mirror is not enabled")
        if config.protection_mirror_s3_bucket is None:
            raise RuntimeError("protection mirror bucket is not configured")
        self._bucket = config.protection_mirror_s3_bucket
        self._prefix = config.protection_mirror_prefix
        self._configured_part_size = config.protection_mirror_multipart_part_bytes
        self._client = create_protection_mirror_s3_client(config)

    def object_path(self, collection_id: str) -> str:
        return f"{self._prefix}/collections/{collection_id}/archive.tar"

    def _collection_prefix(self, collection_id: str) -> str:
        return f"{self._prefix}/collections/{collection_id}/"

    def put_collection_archive_stream_resumable(
        self,
        collection_id: str,
        chunks: Iterable[bytes],
        *,
        content_length: int,
        sha256: str,
        multipart_tracker: ArchiveMultipartUploadTracker | None = None,
    ) -> None:
        object_key = self.object_path(collection_id)
        metadata = {
            _ARCHIVE_BYTES_METADATA: str(content_length),
            _ARCHIVE_SHA256_METADATA: sha256,
        }
        upload_id: str | None = None
        upload_state: ArchiveMultipartUploadState | None = None
        part_number = 1
        completed_parts: list[ArchiveMultipartUploadedPart] = []
        buffer = bytearray()
        part_size = max(_multipart_part_size(content_length), self._configured_part_size)
        expected_part_count = max(1, (content_length + part_size - 1) // part_size)
        uploaded_bytes = 0
        resumed_part_count = 0
        skip_bytes = 0
        size = 0

        if content_length == 0:
            total = sum(len(chunk) for chunk in chunks)
            if total != 0:
                raise ValueError("protection mirror archive byte count mismatch")
            self._client.put_object(
                Bucket=self._bucket,
                Key=object_key,
                Body=b"",
                ContentLength=0,
                Metadata=metadata,
            )
            return

        if multipart_tracker is not None:
            upload_state = multipart_tracker.load_multipart_upload(
                collection_id=collection_id,
                object_path=object_key,
                part_size=part_size,
                content_length=content_length,
                sha256=sha256,
            )
            if upload_state is not None:
                upload_id = upload_state.upload_id
                try:
                    resumed_parts = self._contiguous_uploaded_parts(
                        object_key=object_key,
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
                        "resuming protection mirror archive for %s: upload_id=%s "
                        "parts=%s/%s bytes=%s/%s",
                        object_key,
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
                    "starting protection mirror multipart upload for %s: "
                    "size=%s part_size=%s parts=%s",
                    object_key,
                    content_length,
                    part_size,
                    expected_part_count,
                )
                response = cast(
                    dict[str, Any],
                    self._client.create_multipart_upload(
                        Bucket=self._bucket,
                        Key=object_key,
                        Metadata=metadata,
                    ),
                )
                upload_id = str(response["UploadId"])
                upload_state = ArchiveMultipartUploadState(
                    upload_id=upload_id,
                    object_path=object_key,
                    part_size=part_size,
                    content_length=content_length,
                    sha256=sha256,
                )
                if multipart_tracker is not None:
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
                    Key=object_key,
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
                    "protection mirror multipart upload progress for %s: "
                    "part=%s/%s bytes=%s/%s pct=%.2f",
                    object_key,
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
                raise ValueError("protection mirror archive byte count mismatch")
            if buffer:
                upload_part(bytes(buffer))
                buffer.clear()
            if upload_id is None:
                self._client.put_object(
                    Bucket=self._bucket,
                    Key=object_key,
                    Body=b"",
                    ContentLength=0,
                    Metadata=metadata,
                )
                return
            remote_parts = self._list_uploaded_parts(object_key=object_key, upload_id=upload_id)
            if len(completed_parts) != expected_part_count:
                raise ValueError("protection mirror archive upload is missing parts")
            _validate_recorded_parts_exist_remotely(completed_parts, remote_parts)
            self._client.complete_multipart_upload(
                Bucket=self._bucket,
                Key=object_key,
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
            _LOG.info(
                "completed protection mirror multipart upload for %s: parts=%s bytes=%s",
                object_key,
                expected_part_count,
                content_length,
            )
        except Exception as exc:
            if upload_id is not None and multipart_tracker is None:
                try:
                    self._client.abort_multipart_upload(
                        Bucket=self._bucket,
                        Key=object_key,
                        UploadId=upload_id,
                    )
                except Exception as cleanup_exc:
                    exc.add_note(
                        f"failed to abort protection mirror upload {upload_id}: {cleanup_exc!r}"
                    )
            elif upload_id is not None:
                _LOG.warning(
                    "leaving incomplete protection mirror archive resumable: "
                    "object=%s upload_id=%s uploaded_bytes=%s/%s resumed_parts=%s",
                    object_key,
                    upload_id,
                    uploaded_bytes,
                    content_length,
                    resumed_part_count,
                    exc_info=True,
                )
            raise

    def _list_uploaded_parts(self, *, object_key: str, upload_id: str) -> list[dict[str, object]]:
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

    def iter_collection_archive(
        self,
        collection_id: str,
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
            "Key": self.object_path(collection_id),
        }
        if offset or size is not None:
            request["Range"] = (
                f"bytes={offset}-"
                if size is None
                else f"bytes={offset}-{offset + size - 1}"
            )
        try:
            response = self._client.get_object(**request)
        except self._client.exceptions.ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
                raise
            raise NotFound(f"archive not found in protection mirror: {collection_id}") from exc
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

    def stat_collection_archive(self, collection_id: str) -> ProtectionMirrorArchiveStat | None:
        try:
            head = self._client.head_object(
                Bucket=self._bucket,
                Key=self.object_path(collection_id),
            )
        except self._client.exceptions.ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return None
            raise
        metadata = cast(dict[str, str], head.get("Metadata") or {})
        sha256 = metadata.get(_ARCHIVE_SHA256_METADATA)
        return ProtectionMirrorArchiveStat(
            bytes=int(head.get("ContentLength", 0)),
            sha256=sha256 or None,
        )

    def delete_collection(self, collection_id: str) -> None:
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=self._bucket,
            Prefix=self._collection_prefix(collection_id),
        ):
            contents = page.get("Contents", [])
            if not contents:
                continue
            self._client.delete_objects(
                Bucket=self._bucket,
                Delete={"Objects": [{"Key": entry["Key"]} for entry in contents]},
            )

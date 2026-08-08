from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from botocore.exceptions import ClientError
from time_formats import format_utc_timestamp, utc_now

from riverhog_core.ports.archive_ingress_store import (
    ArchiveObjectIdentityConflict,
    CompletedObjectReceipt,
    MultipartPartReceipt,
    MultipartUpload,
)
from riverhog_core.runtime_config import ArchiveStoreConfig, RuntimeConfig
from riverhog_core.stores.s3_client import create_archive_s3_client
from riverhog_core.throughput import S3TransportTuning


class S3ArchiveMultipartObjectStore:
    """S3 multipart adapter whose completion is create-only.

    The destination key is immutable. ``CompleteMultipartUpload`` carries
    ``If-None-Match: *`` so a concurrent or stale writer cannot replace an archive
    volume. A lost successful completion response is recovered by comparing the
    completed object's immutable identity metadata and byte count.
    """

    def __init__(
        self,
        config: RuntimeConfig,
        store: ArchiveStoreConfig,
        *,
        transport_tuning: S3TransportTuning | None = None,
    ) -> None:
        self._bucket = store.bucket
        self._storage_class = store.storage_class
        self._client = create_archive_s3_client(
            config,
            store,
            tuning=transport_tuning,
        )

    def create_multipart_upload(
        self,
        *,
        object_path: str,
        content_type: str,
        metadata: dict[str, str],
    ) -> MultipartUpload:
        if not object_path or not content_type:
            raise ValueError("multipart object path and content type are required")
        request: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": object_path,
            "ContentType": content_type,
            "Metadata": _normalized_metadata(metadata),
        }
        if self._storage_class:
            request["StorageClass"] = self._storage_class
        response = cast(dict[str, Any], self._client.create_multipart_upload(**request))
        upload_id = str(response.get("UploadId", ""))
        if not upload_id:
            raise RuntimeError("S3 did not return a multipart upload id")
        return MultipartUpload(object_path=object_path, upload_id=upload_id)

    def upload_part(
        self,
        *,
        upload: MultipartUpload,
        number: int,
        content: bytes,
    ) -> MultipartPartReceipt:
        if number < 1 or number > 10_000:
            raise ValueError("S3 multipart part number is outside 1..10000")
        if not content:
            raise ValueError("S3 multipart part must not be empty")
        response = cast(
            dict[str, Any],
            self._client.upload_part(
                Bucket=self._bucket,
                Key=upload.object_path,
                UploadId=upload.upload_id,
                PartNumber=number,
                Body=content,
                ContentLength=len(content),
            ),
        )
        etag = str(response.get("ETag", ""))
        if not etag:
            raise RuntimeError("S3 did not return a multipart part ETag")
        return MultipartPartReceipt(number=number, etag=etag, bytes=len(content))

    def list_parts(self, *, upload: MultipartUpload) -> tuple[MultipartPartReceipt, ...]:
        request: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": upload.object_path,
            "UploadId": upload.upload_id,
        }
        parts: list[MultipartPartReceipt] = []
        while True:
            response = cast(dict[str, Any], self._client.list_parts(**request))
            for raw in response.get("Parts") or ():
                if not isinstance(raw, dict):
                    continue
                parts.append(
                    MultipartPartReceipt(
                        number=int(str(raw["PartNumber"])),
                        etag=str(raw["ETag"]),
                        bytes=int(str(raw["Size"])),
                    )
                )
            if not response.get("IsTruncated"):
                break
            marker = response.get("NextPartNumberMarker")
            if marker is None:
                raise RuntimeError("S3 multipart parts listing omitted its next marker")
            request["PartNumberMarker"] = int(str(marker))
        parts.sort(key=lambda current: current.number)
        return tuple(parts)

    def complete_multipart_upload(
        self,
        *,
        upload: MultipartUpload,
        parts: tuple[MultipartPartReceipt, ...],
        expected_bytes: int,
        expected_metadata: dict[str, str],
    ) -> CompletedObjectReceipt:
        if not parts:
            raise ValueError("cannot complete an S3 multipart upload without parts")
        if [current.number for current in parts] != list(range(1, len(parts) + 1)):
            raise ValueError("S3 multipart part numbers must be contiguous")
        if expected_bytes != sum(current.bytes for current in parts):
            raise ValueError("expected multipart byte count does not match its parts")
        try:
            self._client.complete_multipart_upload(
                Bucket=self._bucket,
                Key=upload.object_path,
                UploadId=upload.upload_id,
                MultipartUpload={
                    "Parts": [
                        {"PartNumber": current.number, "ETag": current.etag} for current in parts
                    ]
                },
                IfNoneMatch="*",
            )
        except ClientError as exc:
            status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
            code = str(exc.response.get("Error", {}).get("Code", ""))
            recoverable = status in {409, 412} or code in {
                "ConditionalRequestConflict",
                "NoSuchUpload",
                "PreconditionFailed",
            }
            if not recoverable:
                if status == 501 or code in {"NotImplemented", "UnsupportedHeader"}:
                    raise RuntimeError(
                        "archive store must support If-None-Match on CompleteMultipartUpload"
                    ) from exc
                raise
            completed = self.head_completed_object(
                object_path=upload.object_path,
                expected_metadata=expected_metadata,
            )
            if completed is None:
                raise RuntimeError(
                    "conditional multipart completion failed and no completed object exists; "
                    "restart the multipart upload"
                ) from exc
            if completed.bytes != expected_bytes:
                raise ArchiveObjectIdentityConflict(
                    "completed archive object byte count differs from the upload checkpoint"
                ) from exc
            return completed

        completed = self.head_completed_object(
            object_path=upload.object_path,
            expected_metadata=expected_metadata,
        )
        if completed is None:
            raise RuntimeError("S3 completion succeeded but the archive object is not readable")
        if completed.bytes != expected_bytes:
            raise RuntimeError("completed S3 archive object length does not match uploaded parts")
        return completed

    def head_completed_object(
        self,
        *,
        object_path: str,
        expected_metadata: dict[str, str],
    ) -> CompletedObjectReceipt | None:
        head = self._head(object_path)
        if head is None:
            return None
        actual_metadata = _normalized_metadata(cast(dict[str, Any], head.get("Metadata") or {}))
        normalized_expected = _normalized_metadata(expected_metadata)
        mismatched = {
            key: (value, actual_metadata.get(key))
            for key, value in normalized_expected.items()
            if actual_metadata.get(key) != value
        }
        if mismatched:
            raise ArchiveObjectIdentityConflict(
                f"archive object already exists with different identity metadata: {object_path}"
            )
        return _completed_receipt(object_path, head)

    def abort_multipart_upload(self, *, upload: MultipartUpload) -> None:
        try:
            self._client.abort_multipart_upload(
                Bucket=self._bucket,
                Key=upload.object_path,
                UploadId=upload.upload_id,
            )
        except ClientError as exc:
            status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if status == 404 or code in {"NoSuchUpload", "NotFound"}:
                return
            raise

    def _head(self, object_path: str) -> dict[str, Any] | None:
        try:
            return cast(
                dict[str, Any],
                self._client.head_object(Bucket=self._bucket, Key=object_path),
            )
        except ClientError as exc:
            status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if status == 404 or code in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise


def _normalized_metadata(value: dict[str, object] | dict[str, str]) -> dict[str, str]:
    return {str(key).casefold(): str(current) for key, current in value.items()}


def _completed_receipt(object_path: str, head: dict[str, Any]) -> CompletedObjectReceipt:
    last_modified = head.get("LastModified")
    completed_at = (
        format_utc_timestamp(last_modified)
        if isinstance(last_modified, datetime)
        else format_utc_timestamp(utc_now())
    )
    version_id = head.get("VersionId")
    etag = head.get("ETag")
    return CompletedObjectReceipt(
        object_path=object_path,
        version_id=str(version_id) if version_id is not None else None,
        etag=str(etag) if etag is not None else None,
        bytes=int(str(head["ContentLength"])),
        completed_at=completed_at,
    )

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, TypedDict, cast

from riverhog_core.collection_archives import CollectionArchivePackage
from riverhog_core.ports.archive_store import (
    ArchiveRestoreStatus,
    ArchiveUploadReceipt,
    CollectionArchiveUploadReceipt,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.stores.s3_support import create_glacier_s3_client

COLLECTION_BYTES_METADATA = "riverhog-collection-bytes"
COLLECTION_SHA256_METADATA = "riverhog-collection-sha256"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MIN_MULTIPART_PART_SIZE = 5 * 1024 * 1024
_MAX_MULTIPART_PART_SIZE = 5 * 1024 * 1024 * 1024
_MAX_MULTIPART_PARTS = 10_000
_MAX_SINGLE_PUT_OBJECT_SIZE = 5 * 1024 * 1024 * 1024
_LOG = logging.getLogger(__name__)


class _RestoreHeader(TypedDict):
    ongoing: bool
    expires_at: str | None


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _multipart_part_size(content_length: int, configured_part_size: int) -> int:
    part_size = max(
        _MIN_MULTIPART_PART_SIZE,
        configured_part_size,
        (content_length + _MAX_MULTIPART_PARTS - 1) // _MAX_MULTIPART_PARTS,
    )
    if part_size > _MAX_MULTIPART_PART_SIZE:
        raise ValueError("collection archive stream exceeds S3 multipart object size limit")
    return part_size


def _should_log_multipart_progress(part_number: int, expected_part_count: int) -> bool:
    return (
        part_number == 1
        or part_number == expected_part_count
        or expected_part_count <= 20
        or part_number % 100 == 0
    )


def _is_chunk_iterable(content: Any) -> bool:
    return (
        isinstance(content, Iterable)
        and not isinstance(content, (bytes, bytearray, memoryview, str))
        and not callable(getattr(content, "read", None))
    )


def _should_use_multipart(*, content: Any, content_length: int) -> bool:
    return content_length > _MAX_SINGLE_PUT_OBJECT_SIZE or (
        _is_chunk_iterable(content) and content_length >= _MIN_MULTIPART_PART_SIZE
    )


def _iter_content_chunks(content: Any) -> Iterator[bytes]:
    if isinstance(content, bytes):
        yield content
        return
    if isinstance(content, bytearray):
        yield bytes(content)
        return
    if isinstance(content, memoryview):
        yield content.tobytes()
        return

    read = getattr(content, "read", None)
    if callable(read):
        while True:
            chunk = read(1024 * 1024)
            if not chunk:
                return
            yield bytes(chunk)

    for chunk in cast(Iterable[Any], content):
        yield bytes(chunk)


def _single_put_body(content: Any) -> Any:
    if isinstance(content, (bytes, bytearray)):
        return bytes(content)
    if isinstance(content, memoryview):
        return content.tobytes()
    if callable(getattr(content, "read", None)):
        return content
    return b"".join(_iter_content_chunks(content))


class S3ArchiveStore:
    def __init__(self, config: RuntimeConfig) -> None:
        self._config = config
        self._bucket = config.glacier_bucket
        self._client = create_glacier_s3_client(config)

    def _collection_object_keys(self, *, collection_id: str) -> dict[str, str]:
        collection_hash = hashlib.sha256(collection_id.encode("utf-8")).hexdigest()
        prefix = self._config.glacier_prefix
        collection_prefix = f"{prefix}/collections/{collection_hash}"
        return {
            "archive": f"{collection_prefix}/archive.tar",
            "manifest": f"{collection_prefix}/manifest.yml",
            "proof": f"{collection_prefix}/manifest.yml.ots",
        }

    def _head_object(self, *, object_key: str) -> dict[str, Any] | None:
        try:
            return cast(
                dict[str, Any],
                self._client.head_object(Bucket=self._bucket, Key=object_key),
            )
        except Exception as exc:
            if _is_missing_object_error(exc):
                return None
            raise

    def _collection_receipt_from_head(
        self,
        *,
        object_key: str,
        head: dict[str, Any],
        expected_bytes: int,
        expected_sha256: str,
        uploaded_at: str | None = None,
    ) -> ArchiveUploadReceipt:
        _validate_uploaded_collection_metadata(
            object_key=object_key,
            head=head,
            expected_bytes=expected_bytes,
            expected_sha256=expected_sha256,
        )
        if self._is_aws_restore_backend():
            _validate_aws_storage_class(
                object_key=object_key,
                head=head,
                expected_storage_class=self._config.glacier_storage_class,
            )
        verified_at = _utc_now()
        return ArchiveUploadReceipt(
            object_path=object_key,
            stored_bytes=int(head.get("ContentLength", 0)),
            backend=self._config.glacier_backend,
            storage_class=self._config.glacier_storage_class,
            uploaded_at=uploaded_at
            or _format_s3_timestamp(
                head.get("LastModified"),
                fallback=verified_at,
            ),
            verified_at=verified_at,
        )

    def upload_collection_archive_package(
        self,
        *,
        collection_id: str,
        package: CollectionArchivePackage,
    ) -> CollectionArchiveUploadReceipt:
        keys = self._collection_object_keys(collection_id=collection_id)
        archive = self._put_collection_package_object(
            object_key=keys["archive"],
            content=package.iter_archive(),
            content_length=package.archive_size,
            sha256=package.archive_sha256,
            kind="archive",
            package=package,
        )
        manifest = self._put_collection_package_object(
            object_key=keys["manifest"],
            content=package.manifest_bytes,
            content_length=len(package.manifest_bytes),
            sha256=package.manifest_sha256,
            kind="manifest",
            package=package,
        )
        proof = self._put_collection_package_object(
            object_key=keys["proof"],
            content=package.proof_bytes,
            content_length=len(package.proof_bytes),
            sha256=package.proof_sha256,
            kind="ots-proof",
            package=package,
        )
        return CollectionArchiveUploadReceipt(
            archive=archive,
            manifest=manifest,
            proof=proof,
            archive_sha256=package.archive_sha256,
            manifest_sha256=package.manifest_sha256,
            proof_sha256=package.proof_sha256,
            archive_format=package.archive_format,
            compression=package.compression,
        )

    def _put_collection_package_object(
        self,
        *,
        object_key: str,
        content: Any,
        content_length: int,
        sha256: str,
        kind: str,
        package: CollectionArchivePackage,
    ) -> ArchiveUploadReceipt:
        existing = self._head_object(object_key=object_key)
        if existing is not None:
            return self._collection_receipt_from_head(
                object_key=object_key,
                head=existing,
                expected_bytes=content_length,
                expected_sha256=sha256,
            )

        uploaded_at = _utc_now()
        extra_args: dict[str, Any] = {
            "Metadata": {
                "riverhog-backend": self._config.glacier_backend,
                "riverhog-storage-class": self._config.glacier_storage_class,
                "riverhog-object-kind": f"collection-{kind}",
                "riverhog-collection-sha256": hashlib.sha256(
                    package.collection_id.encode("utf-8")
                ).hexdigest(),
                "riverhog-archive-format": package.archive_format,
                "riverhog-compression": package.compression,
                "riverhog-archive-bytes": str(content_length),
                "riverhog-archive-sha256": sha256,
                COLLECTION_BYTES_METADATA: str(content_length),
                COLLECTION_SHA256_METADATA: sha256,
            }
        }
        if self._is_aws_restore_backend():
            extra_args["StorageClass"] = self._config.glacier_storage_class
        if _should_use_multipart(content=content, content_length=content_length):
            self._put_collection_package_object_multipart(
                object_key=object_key,
                chunks=_iter_content_chunks(content),
                content_length=content_length,
                extra_args=extra_args,
            )
        else:
            self._client.put_object(
                Bucket=self._bucket,
                Key=object_key,
                Body=_single_put_body(content),
                ContentLength=content_length,
                **extra_args,
            )
        head = cast(
            dict[str, Any],
            self._client.head_object(Bucket=self._bucket, Key=object_key),
        )
        return self._collection_receipt_from_head(
            object_key=object_key,
            head=head,
            expected_bytes=content_length,
            expected_sha256=sha256,
            uploaded_at=uploaded_at,
        )

    def _put_collection_package_object_multipart(
        self,
        *,
        object_key: str,
        chunks: Iterable[bytes],
        content_length: int,
        extra_args: dict[str, Any],
    ) -> None:
        upload_id: str | None = None
        part_number = 1
        uploaded_parts: list[dict[str, object]] = []
        buffer = bytearray()
        part_size = _multipart_part_size(
            content_length,
            self._config.glacier_multipart_part_bytes,
        )
        expected_part_count = (content_length + part_size - 1) // part_size
        uploaded_bytes = 0
        size = 0

        def ensure_upload() -> str:
            nonlocal upload_id
            if upload_id is None:
                _LOG.info(
                    "starting S3 multipart upload for %s: size=%s part_size=%s parts=%s",
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
                        **extra_args,
                    ),
                )
                upload_id = str(response["UploadId"])
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
            uploaded_parts.append(
                {"PartNumber": current_part_number, "ETag": str(response["ETag"])}
            )
            uploaded_bytes += len(body)
            if _should_log_multipart_progress(current_part_number, expected_part_count):
                _LOG.info(
                    "S3 multipart upload progress for %s: part=%s/%s bytes=%s/%s pct=%.2f",
                    object_key,
                    current_part_number,
                    expected_part_count,
                    uploaded_bytes,
                    content_length,
                    (uploaded_bytes / content_length * 100.0) if content_length else 100.0,
                )
            part_number += 1

        try:
            for chunk in chunks:
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

            if size != content_length:
                raise ValueError("collection archive stream byte count mismatch")
            if buffer:
                upload_part(bytes(buffer))
                buffer.clear()
            if upload_id is None:
                self._client.put_object(
                    Bucket=self._bucket,
                    Key=object_key,
                    Body=b"",
                    ContentLength=0,
                    **extra_args,
                )
                return
            self._client.complete_multipart_upload(
                Bucket=self._bucket,
                Key=object_key,
                UploadId=upload_id,
                MultipartUpload={"Parts": uploaded_parts},
            )
            _LOG.info(
                "completed S3 multipart upload for %s: parts=%s bytes=%s",
                object_key,
                len(uploaded_parts),
                uploaded_bytes,
            )
        except Exception as exc:
            if upload_id is not None:
                try:
                    self._client.abort_multipart_upload(
                        Bucket=self._bucket,
                        Key=object_key,
                        UploadId=upload_id,
                    )
                except Exception as cleanup_exc:
                    exc.add_note(
                        f"failed to abort S3 multipart upload {upload_id}: {cleanup_exc!r}"
                    )
            raise

    def request_collection_archive_restore(
        self,
        *,
        collection_id: str,
        object_path: str,
        retrieval_tier: str,
        hold_days: int,
        requested_at: str,
        estimated_ready_at: str,
        manifest_object_path: str | None = None,
        proof_object_path: str | None = None,
    ) -> ArchiveRestoreStatus:
        statuses = [
            self._request_collection_object_restore(
                object_path=current_object_path,
                retrieval_tier=retrieval_tier,
                hold_days=hold_days,
                requested_at=requested_at,
                estimated_ready_at=estimated_ready_at,
            )
            for current_object_path in _collection_restore_paths(
                object_path=object_path,
                manifest_object_path=manifest_object_path,
                proof_object_path=proof_object_path,
            )
        ]
        return _combine_collection_restore_statuses(statuses)

    def _request_collection_object_restore(
        self,
        *,
        object_path: str,
        retrieval_tier: str,
        hold_days: int,
        requested_at: str,
        estimated_ready_at: str,
    ) -> ArchiveRestoreStatus:
        head = self._head_object(object_key=object_path)
        if head is None:
            raise RuntimeError(f"Glacier object is missing: {object_path}")
        _validate_uploaded_collection_metadata(object_key=object_path, head=head)
        if _is_immediately_readable_storage_class(head):
            return ArchiveRestoreStatus(
                state="ready",
                ready_at=requested_at,
                message="Collection archive object is immediately readable.",
            )
        if self._restore_mode() == "auto" and not self._is_aws_restore_backend():
            raise RuntimeError(
                "real Glacier restore requires an AWS S3 archive backend or "
                "RIVERHOG_GLACIER_RECOVERY_RESTORE_MODE=aws"
            )
        try:
            self._client.restore_object(
                Bucket=self._bucket,
                Key=object_path,
                RestoreRequest={
                    "Days": hold_days,
                    "GlacierJobParameters": {"Tier": _aws_restore_tier(retrieval_tier)},
                },
            )
        except Exception as exc:
            restore_error = _restore_request_error_code(exc)
            if restore_error == "ObjectAlreadyInActiveTierError":
                return ArchiveRestoreStatus(
                    state="ready",
                    ready_at=requested_at,
                    message="Collection archive object is already readable.",
                )
            if restore_error != "RestoreAlreadyInProgress":
                raise
        return self._collection_object_restore_status(
            object_path=object_path,
            requested_at=requested_at,
            estimated_ready_at=estimated_ready_at,
            estimated_expires_at=None,
        )

    def get_collection_archive_restore_status(
        self,
        *,
        collection_id: str,
        object_path: str,
        requested_at: str,
        estimated_ready_at: str | None,
        estimated_expires_at: str | None,
        manifest_object_path: str | None = None,
        proof_object_path: str | None = None,
    ) -> ArchiveRestoreStatus:
        statuses = [
            self._collection_object_restore_status(
                object_path=current_object_path,
                requested_at=requested_at,
                estimated_ready_at=estimated_ready_at,
                estimated_expires_at=estimated_expires_at,
            )
            for current_object_path in _collection_restore_paths(
                object_path=object_path,
                manifest_object_path=manifest_object_path,
                proof_object_path=proof_object_path,
            )
        ]
        return _combine_collection_restore_statuses(statuses)

    def _collection_object_restore_status(
        self,
        *,
        object_path: str,
        requested_at: str,
        estimated_ready_at: str | None,
        estimated_expires_at: str | None,
    ) -> ArchiveRestoreStatus:
        head = self._head_object(object_key=object_path)
        if head is None:
            raise RuntimeError(f"Glacier object is missing: {object_path}")
        _validate_uploaded_collection_metadata(object_key=object_path, head=head)
        restore = _parse_restore_header(head.get("Restore"))
        if restore is None:
            if _is_immediately_readable_storage_class(head):
                return ArchiveRestoreStatus(
                    state="ready",
                    ready_at=requested_at,
                    message="Collection archive object is immediately readable.",
                )
            return ArchiveRestoreStatus(
                state="requested",
                ready_at=estimated_ready_at,
                expires_at=estimated_expires_at,
                message="Collection archive restore is still in progress.",
            )
        if restore["ongoing"]:
            return ArchiveRestoreStatus(
                state="requested",
                ready_at=estimated_ready_at,
                expires_at=restore["expires_at"] or estimated_expires_at,
                message="Collection archive restore is still in progress.",
            )
        return ArchiveRestoreStatus(
            state="ready",
            ready_at=_utc_now(),
            expires_at=restore["expires_at"],
            message="Collection archive object is restored and readable.",
        )

    def iter_restored_collection_archive(
        self,
        *,
        collection_id: str,
        object_path: str,
    ) -> Iterator[bytes]:
        head = self._head_object(object_key=object_path)
        if head is None:
            raise RuntimeError(f"Glacier object is missing: {object_path}")
        _validate_uploaded_collection_metadata(object_key=object_path, head=head)
        status = self.get_collection_archive_restore_status(
            collection_id=collection_id,
            object_path=object_path,
            requested_at=_utc_now(),
            estimated_ready_at=None,
            estimated_expires_at=None,
        )
        if status.state != "ready":
            raise RuntimeError(f"Glacier object is not restored yet: {object_path}")
        response = self._client.get_object(Bucket=self._bucket, Key=object_path)
        body = response["Body"]
        try:
            yield from body.iter_chunks(chunk_size=1024 * 1024)
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()

    def read_restored_collection_archive_manifest(
        self,
        *,
        collection_id: str,
        object_path: str,
    ) -> bytes:
        return self._read_restored_collection_object(
            collection_id=collection_id,
            object_path=object_path,
        )

    def read_restored_collection_archive_proof(
        self,
        *,
        collection_id: str,
        object_path: str,
    ) -> bytes:
        return self._read_restored_collection_object(
            collection_id=collection_id,
            object_path=object_path,
        )

    def _read_restored_collection_object(
        self,
        *,
        collection_id: str,
        object_path: str,
    ) -> bytes:
        head = self._head_object(object_key=object_path)
        if head is None:
            raise RuntimeError(f"Glacier object is missing: {object_path}")
        _validate_uploaded_collection_metadata(object_key=object_path, head=head)
        status = self.get_collection_archive_restore_status(
            collection_id=collection_id,
            object_path=object_path,
            requested_at=_utc_now(),
            estimated_ready_at=None,
            estimated_expires_at=None,
        )
        if status.state != "ready":
            raise RuntimeError(f"Glacier object is not restored yet: {object_path}")
        response = self._client.get_object(Bucket=self._bucket, Key=object_path)
        body = response["Body"]
        try:
            return cast(bytes, body.read())
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()

    def cleanup_collection_archive_restore(
        self,
        *,
        collection_id: str,
        object_path: str,
        manifest_object_path: str | None = None,
        proof_object_path: str | None = None,
    ) -> None:
        return

    def _restore_mode(self) -> str:
        mode = self._config.glacier_recovery_restore_mode
        if mode != "auto":
            return mode
        return "auto"

    def _is_aws_restore_backend(self) -> bool:
        endpoint = self._config.glacier_endpoint_url.casefold()
        return self._config.glacier_backend.casefold() == "aws" or "amazonaws.com" in endpoint


def _collection_restore_paths(
    *,
    object_path: str,
    manifest_object_path: str | None,
    proof_object_path: str | None,
) -> tuple[str, ...]:
    return tuple(
        path for path in (object_path, manifest_object_path, proof_object_path) if path is not None
    )


def _combine_collection_restore_statuses(
    statuses: list[ArchiveRestoreStatus],
) -> ArchiveRestoreStatus:
    if any(status.state == "expired" for status in statuses):
        return ArchiveRestoreStatus(state="expired")
    if statuses and all(status.state == "ready" for status in statuses):
        return ArchiveRestoreStatus(
            state="ready",
            ready_at=_max_timestamp(status.ready_at for status in statuses),
            expires_at=_min_timestamp(status.expires_at for status in statuses),
            message="Collection archive package objects are restored and readable.",
        )
    return ArchiveRestoreStatus(
        state="requested",
        ready_at=_max_timestamp(status.ready_at for status in statuses),
        expires_at=_min_timestamp(status.expires_at for status in statuses),
        message="Collection archive package restore is still in progress.",
    )


def _max_timestamp(values: Iterable[str | None]) -> str | None:
    candidates = [value for value in values if value is not None]
    if not candidates:
        return None
    return max(candidates)


def _min_timestamp(values: Iterable[str | None]) -> str | None:
    candidates = [value for value in values if value is not None]
    if not candidates:
        return None
    return min(candidates)


def _head_metadata(head: dict[str, Any]) -> dict[str, str]:
    metadata = head.get("Metadata", {})
    if not isinstance(metadata, dict):
        return {}
    return {str(key).lower(): str(value) for key, value in metadata.items()}


def _validate_uploaded_collection_metadata(
    *,
    object_key: str,
    head: dict[str, Any],
    expected_bytes: int | None = None,
    expected_sha256: str | None = None,
) -> None:
    metadata = _head_metadata(head)
    stored_bytes = int(head.get("ContentLength", 0))
    metadata_bytes = metadata.get(COLLECTION_BYTES_METADATA)
    metadata_sha256 = metadata.get(COLLECTION_SHA256_METADATA)
    if metadata_bytes is None or metadata_sha256 is None:
        raise RuntimeError(
            f"Glacier object is missing collection validation metadata: {object_key}"
        )
    try:
        collection_bytes = int(metadata_bytes)
    except ValueError as exc:
        raise RuntimeError(
            f"Glacier object has invalid collection byte metadata: {object_key}"
        ) from exc
    if collection_bytes != stored_bytes:
        raise RuntimeError(
            f"Glacier object collection byte metadata does not match size: {object_key}"
        )
    if expected_bytes is not None and collection_bytes != expected_bytes:
        raise RuntimeError(
            f"Glacier object size does not match collection package member: {object_key}"
        )
    if not _SHA256_RE.fullmatch(metadata_sha256):
        raise RuntimeError(f"Glacier object has invalid collection sha256 metadata: {object_key}")
    if expected_sha256 is not None and metadata_sha256 != expected_sha256:
        raise RuntimeError(
            f"Glacier object sha256 does not match collection package member: {object_key}"
        )


def _format_s3_timestamp(value: object, *, fallback: str) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return fallback


def _parse_restore_header(value: object) -> _RestoreHeader | None:
    if value is None:
        return None
    text = str(value)
    ongoing_match = re.search(r'ongoing-request="(true|false)"', text)
    if ongoing_match is None:
        return None
    expires_at: str | None = None
    expiry_match = re.search(r'expiry-date="([^"]+)"', text)
    if expiry_match is not None:
        expires_at = _format_s3_timestamp(
            parsedate_to_datetime(expiry_match.group(1)),
            fallback=expiry_match.group(1),
        )
    return {
        "ongoing": ongoing_match.group(1) == "true",
        "expires_at": expires_at,
    }


def _is_immediately_readable_storage_class(head: dict[str, Any]) -> bool:
    storage_class = _normalized_s3_storage_class(head)
    return storage_class in {"", "STANDARD", "REDUCED_REDUNDANCY", "INTELLIGENT_TIERING"}


def _normalized_s3_storage_class(head: dict[str, Any]) -> str:
    return str(head.get("StorageClass", "")).strip().upper()


def _configured_s3_storage_class(value: str) -> str:
    normalized = value.strip().upper()
    if normalized in {"", "STANDARD"}:
        return "STANDARD"
    return normalized


def _validate_aws_storage_class(
    *,
    object_key: str,
    head: dict[str, Any],
    expected_storage_class: str,
) -> None:
    expected = _configured_s3_storage_class(expected_storage_class)
    actual = _normalized_s3_storage_class(head) or "STANDARD"
    if actual == expected:
        return
    raise RuntimeError(
        "existing AWS Glacier object storage class does not match configured "
        f"RIVERHOG_GLACIER_STORAGE_CLASS for {object_key}: expected {expected}, got {actual}. "
        "Delete the stale object or choose a fresh RIVERHOG_GLACIER_PREFIX before rerunning."
    )


def _aws_restore_tier(value: str) -> str:
    if value == "standard":
        return "Standard"
    return "Bulk"


def _is_missing_object_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error", {})
    if not isinstance(error, dict):
        return False
    code = str(error.get("Code", "")).strip()
    return code in {"NoSuchKey", "404", "NotFound"}


def _restore_request_error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    error = response.get("Error", {})
    if not isinstance(error, dict):
        return None
    code = str(error.get("Code", "")).strip()
    return code or None

from __future__ import annotations

import hashlib
import re
import tempfile
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, TypedDict, cast

from arc_core.collection_archives import CollectionArchivePackage
from arc_core.ports.archive_store import (
    ArchiveRestoreStatus,
    ArchiveUploadReceipt,
    CollectionArchiveUploadReceipt,
)
from arc_core.runtime_config import RuntimeConfig
from arc_core.stores.s3_support import create_glacier_s3_client

COLLECTION_BYTES_METADATA = "arc-collection-bytes"
COLLECTION_SHA256_METADATA = "arc-collection-sha256"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class _RestoreHeader(TypedDict):
    ongoing: bool
    expires_at: str | None


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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
        with tempfile.TemporaryFile() as archive_body:
            for chunk in package.iter_archive():
                archive_body.write(chunk)
            archive_body.seek(0)
            archive = self._put_collection_package_object(
                object_key=keys["archive"],
                content=archive_body,
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
                "arc-backend": self._config.glacier_backend,
                "arc-storage-class": self._config.glacier_storage_class,
                "arc-object-kind": f"collection-{kind}",
                "arc-collection-sha256": hashlib.sha256(
                    package.collection_id.encode("utf-8")
                ).hexdigest(),
                "arc-archive-format": package.archive_format,
                "arc-compression": package.compression,
                "arc-archive-bytes": str(content_length),
                "arc-archive-sha256": sha256,
                COLLECTION_BYTES_METADATA: str(content_length),
                COLLECTION_SHA256_METADATA: sha256,
            }
        }
        if self._is_aws_restore_backend():
            extra_args["StorageClass"] = self._config.glacier_storage_class
        self._client.put_object(
            Bucket=self._bucket,
            Key=object_key,
            Body=content,
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
                "ARC_GLACIER_RECOVERY_RESTORE_MODE=aws"
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
        path
        for path in (object_path, manifest_object_path, proof_object_path)
        if path is not None
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
        f"ARC_GLACIER_STORAGE_CLASS for {object_key}: expected {expected}, got {actual}. "
        "Delete the stale object or choose a fresh ARC_GLACIER_PREFIX before rerunning."
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

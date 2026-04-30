from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, TypedDict, cast

from arc_core.iso.streaming import build_iso_cmd_from_root, validate_iso_image
from arc_core.ports.archive_store import ArchiveRestoreStatus, ArchiveUploadReceipt
from arc_core.runtime_config import RuntimeConfig
from arc_core.stores.s3_support import create_glacier_s3_client

ISO_BYTES_METADATA = "arc-iso-bytes"
ISO_SHA256_METADATA = "arc-iso-sha256"
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

    def _object_key(self, *, image_id: str, filename: str) -> str:
        suffix = Path(filename).suffix or ".iso"
        return f"{self._config.glacier_prefix}/{image_id}/{image_id}{suffix}"

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

    def _receipt_from_head(
        self,
        *,
        object_key: str,
        head: dict[str, Any],
        uploaded_at: str | None = None,
        expected_bytes: int | None = None,
        expected_sha256: str | None = None,
    ) -> ArchiveUploadReceipt:
        _validate_uploaded_iso_metadata(
            object_key=object_key,
            head=head,
            expected_bytes=expected_bytes,
            expected_sha256=expected_sha256,
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

    def upload_finalized_image(
        self,
        *,
        image_id: str,
        filename: str,
        image_root: Path,
    ) -> ArchiveUploadReceipt:
        object_key = self._object_key(image_id=image_id, filename=filename)
        existing = self._head_object(object_key=object_key)
        if existing is not None:
            return self._receipt_from_head(object_key=object_key, head=existing)

        uploaded_at = _utc_now()

        with tempfile.TemporaryDirectory(prefix="arc-glacier-upload-") as tmpdir:
            iso_path = Path(tmpdir) / f"{image_id}.iso"
            with iso_path.open("wb") as handle:
                proc = subprocess.run(
                    build_iso_cmd_from_root(image_root=image_root, volume_id=image_id),
                    stdout=handle,
                    stderr=subprocess.PIPE,
                    check=False,
                )
            if proc.returncode != 0:
                detail = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
                raise RuntimeError(detail or f"xorriso exited {proc.returncode}")
            validate_iso_image(iso_path)
            iso_bytes = iso_path.stat().st_size
            iso_sha256 = _file_sha256(iso_path)

            # The canonical Garage harness does not emulate Glacier storage-class
            # semantics, so the runtime records the intended class in Riverhog's
            # catalog and object metadata instead of depending on backend support.
            extra_args: dict[str, Any] = {
                "Metadata": {
                    "arc-backend": self._config.glacier_backend,
                    "arc-storage-class": self._config.glacier_storage_class,
                    ISO_BYTES_METADATA: str(iso_bytes),
                    ISO_SHA256_METADATA: iso_sha256,
                }
            }
            if self._is_aws_restore_backend():
                extra_args["StorageClass"] = self._config.glacier_storage_class
            self._client.upload_file(
                str(iso_path),
                self._bucket,
                object_key,
                ExtraArgs=extra_args,
            )
            head = cast(
                dict[str, Any],
                self._client.head_object(Bucket=self._bucket, Key=object_key),
            )

        return self._receipt_from_head(
            object_key=object_key,
            head=head,
            uploaded_at=uploaded_at,
            expected_bytes=iso_bytes,
            expected_sha256=iso_sha256,
        )

    def request_finalized_image_restore(
        self,
        *,
        image_id: str,
        object_path: str,
        retrieval_tier: str,
        hold_days: int,
        requested_at: str,
        estimated_ready_at: str,
    ) -> ArchiveRestoreStatus:
        head = self._head_object(object_key=object_path)
        if head is None:
            raise RuntimeError(f"Glacier object is missing: {object_path}")
        _validate_uploaded_iso_metadata(object_key=object_path, head=head)
        if _is_immediately_readable_storage_class(head):
            return ArchiveRestoreStatus(
                state="ready",
                ready_at=requested_at,
                message="Archive object is immediately readable.",
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
                    message="Archive object is already readable.",
                )
            if restore_error != "RestoreAlreadyInProgress":
                raise
        return self.get_finalized_image_restore_status(
            image_id=image_id,
            object_path=object_path,
            requested_at=requested_at,
            estimated_ready_at=estimated_ready_at,
            estimated_expires_at=None,
        )

    def get_finalized_image_restore_status(
        self,
        *,
        image_id: str,
        object_path: str,
        requested_at: str,
        estimated_ready_at: str | None,
        estimated_expires_at: str | None,
    ) -> ArchiveRestoreStatus:
        head = self._head_object(object_key=object_path)
        if head is None:
            raise RuntimeError(f"Glacier object is missing: {object_path}")
        _validate_uploaded_iso_metadata(object_key=object_path, head=head)
        restore = _parse_restore_header(head.get("Restore"))
        if restore is None:
            if _is_immediately_readable_storage_class(head):
                return ArchiveRestoreStatus(
                    state="ready",
                    ready_at=requested_at,
                    message="Archive object is immediately readable.",
                )
            return ArchiveRestoreStatus(
                state="requested",
                ready_at=estimated_ready_at,
                message="Archive restore is still in progress.",
            )
        if restore["ongoing"]:
            return ArchiveRestoreStatus(
                state="requested",
                ready_at=estimated_ready_at,
                expires_at=restore["expires_at"],
                message="Archive restore is still in progress.",
            )
        return ArchiveRestoreStatus(
            state="ready",
            ready_at=_utc_now(),
            expires_at=restore["expires_at"],
            message="Archive object is restored and readable.",
        )

    def iter_restored_finalized_image(
        self,
        *,
        image_id: str,
        object_path: str,
    ) -> Iterator[bytes]:
        head = self._head_object(object_key=object_path)
        if head is None:
            raise RuntimeError(f"Glacier object is missing: {object_path}")
        _validate_uploaded_iso_metadata(object_key=object_path, head=head)
        status = self.get_finalized_image_restore_status(
            image_id=image_id,
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

    def cleanup_finalized_image_restore(
        self,
        *,
        image_id: str,
        object_path: str,
    ) -> None:
        # AWS S3 does not expose deletion of only the temporary restored copy without
        # deleting the archived object. Completion records Riverhog cleanup and lets
        # the restore Days window/lifecycle expire the temporary Standard data.
        return

    def _restore_mode(self) -> str:
        mode = self._config.glacier_recovery_restore_mode
        if mode != "auto":
            return mode
        return "auto"

    def _is_aws_restore_backend(self) -> bool:
        endpoint = self._config.glacier_endpoint_url.casefold()
        return self._config.glacier_backend.casefold() == "aws" or "amazonaws.com" in endpoint


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _head_metadata(head: dict[str, Any]) -> dict[str, str]:
    metadata = head.get("Metadata", {})
    if not isinstance(metadata, dict):
        return {}
    return {str(key).lower(): str(value) for key, value in metadata.items()}


def _validate_uploaded_iso_metadata(
    *,
    object_key: str,
    head: dict[str, Any],
    expected_bytes: int | None = None,
    expected_sha256: str | None = None,
) -> None:
    metadata = _head_metadata(head)
    stored_bytes = int(head.get("ContentLength", 0))
    metadata_bytes = metadata.get(ISO_BYTES_METADATA)
    metadata_sha256 = metadata.get(ISO_SHA256_METADATA)
    if metadata_bytes is None or metadata_sha256 is None:
        raise RuntimeError(f"Glacier object is missing ISO validation metadata: {object_key}")
    try:
        iso_bytes = int(metadata_bytes)
    except ValueError as exc:
        raise RuntimeError(
            f"Glacier object has invalid ISO byte metadata: {object_key}"
        ) from exc
    if iso_bytes != stored_bytes:
        raise RuntimeError(f"Glacier object ISO byte metadata does not match size: {object_key}")
    if expected_bytes is not None and iso_bytes != expected_bytes:
        raise RuntimeError(f"Glacier object size does not match validated ISO: {object_key}")
    if not _SHA256_RE.fullmatch(metadata_sha256):
        raise RuntimeError(f"Glacier object has invalid ISO sha256 metadata: {object_key}")
    if expected_sha256 is not None and metadata_sha256 != expected_sha256:
        raise RuntimeError(f"Glacier object sha256 does not match validated ISO: {object_key}")


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
    storage_class = str(head.get("StorageClass", "")).upper()
    return storage_class in {"", "STANDARD", "REDUCED_REDUNDANCY", "INTELLIGENT_TIERING"}


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

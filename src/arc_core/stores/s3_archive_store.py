from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from arc_core.iso.streaming import build_iso_cmd_from_root, validate_iso_image
from arc_core.ports.archive_store import ArchiveUploadReceipt
from arc_core.runtime_config import RuntimeConfig
from arc_core.stores.s3_support import create_glacier_s3_client

ISO_BYTES_METADATA = "arc-iso-bytes"
ISO_SHA256_METADATA = "arc-iso-sha256"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


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
            self._client.upload_file(
                str(iso_path),
                self._bucket,
                object_key,
                ExtraArgs={
                    "Metadata": {
                        "arc-backend": self._config.glacier_backend,
                        "arc-storage-class": self._config.glacier_storage_class,
                        ISO_BYTES_METADATA: str(iso_bytes),
                        ISO_SHA256_METADATA: iso_sha256,
                    }
                },
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


def _is_missing_object_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error", {})
    if not isinstance(error, dict):
        return False
    code = str(error.get("Code", "")).strip()
    return code in {"NoSuchKey", "404", "NotFound"}

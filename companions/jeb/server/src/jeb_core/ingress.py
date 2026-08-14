from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import stat
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from jeb_core.domain.models import JebIngressConfig
from jeb_core.domain.sources import SourceRegistryError
from jeb_core.persistence.source_registry import SourceRegistry

TUS_SOURCE_METADATA = "jeb_source"
TUS_PATH_METADATA = "jeb_path"
TUS_SIGNATURE_METADATA = "jeb_signature"
TUS_PAYLOAD_SHA256_METADATA = "jeb_payload_sha256"
TUS_PROVENANCE_SHA256_METADATA = "jeb_provenance_sha256"


class JebIngressError(RuntimeError):
    pass


class JebIngressAuthenticationError(JebIngressError):
    pass


class JebIngressCollisionError(JebIngressError):
    pass


def normalize_tus_upload_id(raw: object) -> str:
    value = str(raw or "")
    try:
        normalized = uuid.UUID(value).hex
    except (ValueError, AttributeError) as exc:
        raise JebIngressError("Jeb TUS upload has an invalid ID") from exc
    if value != normalized:
        raise JebIngressError("Jeb TUS upload has an invalid ID")
    return normalized


@dataclass(frozen=True)
class PreparedTusUpload:
    upload_id: str
    source: str
    relative_path: str
    size: int
    payload_sha256: str
    provenance_sha256: str
    signature: str

    def hook_metadata(self) -> dict[str, str]:
        return {
            TUS_SOURCE_METADATA: self.source,
            TUS_PATH_METADATA: self.relative_path,
            TUS_PAYLOAD_SHA256_METADATA: self.payload_sha256,
            TUS_PROVENANCE_SHA256_METADATA: self.provenance_sha256,
            TUS_SIGNATURE_METADATA: self.signature,
        }


@dataclass(frozen=True)
class ValidatedTusUpload:
    upload_id: str
    source_id: str
    relative_path: str
    size: int
    payload_sha256: str
    provenance_sha256: str
    source: Path
    info: Path
    destination: Path


@dataclass(frozen=True)
class IncompleteTusUpload:
    upload_id: str
    source_id: str
    bytes: int
    age_seconds: int


@dataclass(frozen=True)
class IncompleteTusUploadScan:
    uploads: tuple[IncompleteTusUpload, ...]
    invalid_records: int = 0
    scan_error: str | None = None


def scan_incomplete_tus_uploads(
    config: JebIngressConfig,
    registry: SourceRegistry,
    *,
    now: float | None = None,
) -> IncompleteTusUploadScan:
    if not config.tus_staging_dir.exists():
        return IncompleteTusUploadScan(uploads=())
    observed_at = time.time() if now is None else now
    uploads: list[IncompleteTusUpload] = []
    invalid_records = 0
    try:
        info_paths = sorted(config.tus_staging_dir.glob("*.info"))
    except OSError as exc:
        return IncompleteTusUploadScan(
            uploads=(),
            scan_error=exc.__class__.__name__,
        )
    for info_path in info_paths:
        try:
            payload = json.loads(info_path.read_text(encoding="utf-8"))
            if not isinstance(payload, Mapping):
                raise ValueError("upload record must be an object")
            upload_id = normalize_tus_upload_id(payload.get("ID"))
            if upload_id != info_path.name.removesuffix(".info"):
                raise ValueError("upload record has an invalid ID")
            size = _upload_size(payload.get("Size"))
            offset = _upload_size(payload.get("Offset"))
            if offset >= size:
                continue
            metadata = payload.get("MetaData")
            if not isinstance(metadata, Mapping):
                raise ValueError("upload record has no metadata")
            source_id = str(metadata.get(TUS_SOURCE_METADATA) or "")
            relative_path = normalize_landing_path(metadata.get(TUS_PATH_METADATA))
            payload_sha256 = _sha256_metadata(metadata.get(TUS_PAYLOAD_SHA256_METADATA), "payload")
            provenance_sha256 = _sha256_metadata(
                metadata.get(TUS_PROVENANCE_SHA256_METADATA), "provenance"
            )
            signature = str(metadata.get(TUS_SIGNATURE_METADATA) or "")
            expected_signature = _upload_signature(
                signing_key=registry.signing_key(source_id),
                upload_id=upload_id,
                source=source_id,
                relative_path=relative_path,
                size=size,
                payload_sha256=payload_sha256,
                provenance_sha256=provenance_sha256,
            )
            if not signature or not secrets.compare_digest(signature, expected_signature):
                raise ValueError("upload record is not authentic")
            upload_file = config.tus_staging_dir / upload_id
            source_stat = upload_file.stat()
            if not upload_file.is_file():
                raise ValueError("upload data is not a file")
            latest_activity = max(info_path.stat().st_mtime, source_stat.st_mtime)
            uploads.append(
                IncompleteTusUpload(
                    upload_id=upload_id,
                    source_id=source_id,
                    bytes=max(offset, source_stat.st_size),
                    age_seconds=max(0, int(observed_at - latest_activity)),
                )
            )
        except (JebIngressError, OSError, SourceRegistryError, ValueError):
            invalid_records += 1
    return IncompleteTusUploadScan(
        uploads=tuple(uploads),
        invalid_records=invalid_records,
    )


def incomplete_tus_upload_status(
    config: JebIngressConfig,
    registry: SourceRegistry,
    *,
    now: float | None = None,
) -> dict[str, object]:
    scan = scan_incomplete_tus_uploads(config, registry, now=now)
    stale = [
        upload
        for upload in scan.uploads
        if upload.age_seconds >= config.tus_incomplete_max_age_seconds
    ]
    return {
        "total": len(scan.uploads),
        "bytes": sum(upload.bytes for upload in scan.uploads),
        "oldest_age_seconds": max(
            (upload.age_seconds for upload in scan.uploads),
            default=0,
        ),
        "stale": len(stale),
        "stale_bytes": sum(upload.bytes for upload in stale),
        "max_age_seconds": config.tus_incomplete_max_age_seconds,
        "invalid_records": scan.invalid_records,
        "scan_error": scan.scan_error,
    }


def authenticate_tus_source(registry: SourceRegistry, authorization: str | None) -> str:
    source, supplied_password = _basic_credentials(authorization)
    try:
        registry.authenticate(source, supplied_password, adapter="tus")
    except SourceRegistryError as exc:
        raise JebIngressAuthenticationError("invalid Jeb ingress credentials") from exc
    return source


def prepare_tus_upload(
    config: JebIngressConfig,
    registry: SourceRegistry,
    *,
    authorization: str | None,
    metadata: Mapping[str, object],
    size: object,
) -> PreparedTusUpload:
    source = authenticate_tus_source(registry, authorization)
    parsed_size = _upload_size(size)
    relative_path = normalize_landing_path(metadata.get("path") or metadata.get("filename"))
    payload_sha256 = _sha256_metadata(metadata.get("sha256"), "payload")
    provenance_sha256 = _sha256_metadata(metadata.get("provenance_sha256"), "provenance")
    upload_id = uuid.uuid4().hex
    return PreparedTusUpload(
        upload_id=upload_id,
        source=source,
        relative_path=relative_path,
        size=parsed_size,
        payload_sha256=payload_sha256,
        provenance_sha256=provenance_sha256,
        signature=_upload_signature(
            signing_key=registry.signing_key(source),
            upload_id=upload_id,
            source=source,
            relative_path=relative_path,
            size=parsed_size,
            payload_sha256=payload_sha256,
            provenance_sha256=provenance_sha256,
        ),
    )


def validate_tus_upload(
    config: JebIngressConfig,
    registry: SourceRegistry,
    *,
    upload: Mapping[str, object],
) -> ValidatedTusUpload:
    upload_id = normalize_tus_upload_id(upload.get("ID"))
    size = _upload_size(upload.get("Size"))
    if _upload_size(upload.get("Offset")) != size:
        raise JebIngressError("Jeb TUS upload is not complete")

    metadata = upload.get("MetaData")
    if not isinstance(metadata, Mapping):
        raise JebIngressError("Jeb TUS upload metadata is missing")
    source_id = str(metadata.get(TUS_SOURCE_METADATA) or "")
    relative_path = normalize_landing_path(metadata.get(TUS_PATH_METADATA))
    payload_sha256 = _sha256_metadata(metadata.get(TUS_PAYLOAD_SHA256_METADATA), "payload")
    provenance_sha256 = _sha256_metadata(metadata.get(TUS_PROVENANCE_SHA256_METADATA), "provenance")
    signature = str(metadata.get(TUS_SIGNATURE_METADATA) or "")
    expected_signature = _upload_signature(
        signing_key=registry.signing_key(source_id),
        upload_id=upload_id,
        source=source_id,
        relative_path=relative_path,
        size=size,
        payload_sha256=payload_sha256,
        provenance_sha256=provenance_sha256,
    )
    if not signature or not secrets.compare_digest(signature, expected_signature):
        raise JebIngressAuthenticationError("Jeb TUS upload metadata is not authentic")

    storage = upload.get("Storage")
    if not isinstance(storage, Mapping) or storage.get("Type") != "filestore":
        raise JebIngressError("Jeb TUS ingress requires filestore staging")
    raw_source = config.tus_staging_dir / upload_id
    raw_info = config.tus_staging_dir / f"{upload_id}.info"
    if raw_source.is_symlink() or raw_info.is_symlink():
        raise JebIngressError("Jeb TUS staging records must not be symlinks")
    expected_source = raw_source.resolve()
    expected_info = raw_info.resolve()
    reported_source = Path(str(storage.get("Path") or "")).resolve()
    reported_info = Path(str(storage.get("InfoPath") or "")).resolve()
    if reported_source != expected_source or reported_info != expected_info:
        raise JebIngressError("Jeb TUS upload escaped its staging directory")

    destination = _landing_destination(
        config,
        registry,
        source=source_id,
        relative_path=relative_path,
    )
    return ValidatedTusUpload(
        upload_id=upload_id,
        source_id=source_id,
        relative_path=relative_path,
        size=size,
        payload_sha256=payload_sha256,
        provenance_sha256=provenance_sha256,
        source=expected_source,
        info=expected_info,
        destination=destination,
    )


class JebLandingPublisher:
    def __init__(self, config: JebIngressConfig) -> None:
        self._landing_dir = config.landing_dir.resolve()
        self._staging_dir = config.tus_staging_dir.resolve()

    def publish(
        self,
        *,
        upload_id: str,
        source: Path,
        info: Path,
        destination: Path,
        size: int,
    ) -> Path:
        upload_id = normalize_tus_upload_id(upload_id)
        source = source.resolve()
        info = info.resolve()
        destination = destination.resolve()
        self._require_within(source, self._staging_dir, label="source")
        self._require_within(info, self._staging_dir, label="info")
        self._require_within(destination, self._landing_dir, label="destination")
        relative_destination = destination.relative_to(self._landing_dir).as_posix()

        try:
            source_mode = os.lstat(source).st_mode
            info_mode = os.lstat(info).st_mode
        except FileNotFoundError as exc:
            raise JebIngressError("completed Jeb TUS upload is missing from staging") from exc
        if not stat.S_ISREG(source_mode) or not stat.S_ISREG(info_mode):
            raise JebIngressError("completed Jeb TUS upload staging must contain regular files")
        if not source.is_file():
            raise JebIngressError("completed Jeb TUS upload is missing from staging")
        if source.stat().st_size != size:
            raise JebIngressError("completed Jeb TUS upload size does not match its record")

        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            try:
                same_upload = os.path.samefile(source, destination)
            except FileNotFoundError:
                same_upload = False
            if not same_upload:
                raise JebIngressCollisionError(
                    f"Jeb landing path already exists: {relative_destination}"
                )
        else:
            try:
                os.link(source, destination)
            except FileExistsError as exc:
                raise JebIngressCollisionError(
                    f"Jeb landing path already exists: {relative_destination}"
                ) from exc
            except OSError as exc:
                raise JebIngressError(
                    "Jeb TUS staging and landing directories must share a hard-link-capable "
                    "filesystem"
                ) from exc

        return destination

    @staticmethod
    def _require_within(path: Path, root: Path, *, label: str) -> None:
        if not path.is_relative_to(root):
            raise JebIngressError(f"Jeb ingress {label} escaped its configured root")

    @staticmethod
    def cleanup_staging(source: Path, info: Path) -> None:
        source.unlink(missing_ok=True)
        info.unlink(missing_ok=True)


def normalize_landing_path(raw: object) -> str:
    value = str(raw or "").strip()
    if not value or "\\" in value:
        raise JebIngressError("Jeb TUS upload requires a relative path or filename")
    path = PurePosixPath(value)
    if path.is_absolute() or value.endswith("/"):
        raise JebIngressError("Jeb TUS upload path must be a relative file path")
    if any(part in {"", ".", ".."} or part.startswith(".") for part in path.parts):
        raise JebIngressError("Jeb TUS upload path must be normalized and visible")
    return path.as_posix()


def _landing_destination(
    config: JebIngressConfig,
    registry: SourceRegistry,
    *,
    source: str,
    relative_path: str,
) -> Path:
    try:
        configured = registry.get(source)
    except SourceRegistryError as exc:
        raise JebIngressAuthenticationError("Jeb TUS source is not enabled") from exc
    if not configured.enabled or "tus" not in configured.adapters:
        raise JebIngressAuthenticationError("Jeb TUS source is not enabled")
    normalized_path = PurePosixPath(normalize_landing_path(relative_path))
    source_root = configured.path.resolve()
    destination = (configured.path / normalized_path).resolve()
    if not destination.is_relative_to(source_root):
        raise JebIngressError("Jeb TUS upload escaped its source landing directory")
    return destination


def _basic_credentials(authorization: str | None) -> tuple[str, str]:
    scheme, _, encoded = str(authorization or "").partition(" ")
    if scheme.casefold() != "basic" or not encoded:
        raise JebIngressAuthenticationError("Jeb TUS ingress requires HTTP Basic credentials")
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise JebIngressAuthenticationError("invalid Jeb ingress credentials") from exc
    source, separator, password = decoded.partition(":")
    if not separator or not source or not password:
        raise JebIngressAuthenticationError("invalid Jeb ingress credentials")
    return source, password


def _upload_size(raw: object) -> int:
    if isinstance(raw, bool):
        raise JebIngressError("Jeb TUS upload requires a known non-negative size")
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str):
        try:
            value = int(raw)
        except ValueError as exc:
            raise JebIngressError("Jeb TUS upload requires a known non-negative size") from exc
    else:
        raise JebIngressError("Jeb TUS upload requires a known non-negative size")
    if value < 0:
        raise JebIngressError("Jeb TUS upload requires a known non-negative size")
    return value


def _upload_signature(
    *,
    signing_key: str,
    upload_id: str,
    source: str,
    relative_path: str,
    size: int,
    payload_sha256: str,
    provenance_sha256: str,
) -> str:
    message = "\0".join(
        (
            upload_id,
            source,
            relative_path,
            str(size),
            payload_sha256,
            provenance_sha256,
        )
    ).encode("utf-8")
    return hmac.new(signing_key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _sha256_metadata(value: object, label: str) -> str:
    text = str(value or "")
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise JebIngressError(f"Jeb TUS {label} SHA-256 is invalid")
    return text

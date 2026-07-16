from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

TUS_ACCOUNT_METADATA = "jeb_account"
TUS_PATH_METADATA = "jeb_path"
TUS_SIGNATURE_METADATA = "jeb_signature"


class JebIngressError(RuntimeError):
    pass


class JebIngressAuthenticationError(JebIngressError):
    pass


class JebIngressCollisionError(JebIngressError):
    pass


@dataclass(frozen=True)
class JebIngressConfig:
    landing_dir: Path
    tus_staging_dir: Path
    ftp_accounts: tuple[str, ...] = ()
    tus_accounts: tuple[str, ...] = ()
    account_passwords: Mapping[str, str] = field(default_factory=dict, repr=False)

    def tus_password(self, account: str) -> str:
        if account not in self.tus_accounts:
            raise JebIngressAuthenticationError("Jeb TUS account is not enabled")
        password = self.account_passwords.get(account, "")
        if not password:
            raise JebIngressAuthenticationError("Jeb TUS account has no password")
        return password


@dataclass(frozen=True)
class PreparedTusUpload:
    upload_id: str
    account: str
    relative_path: str
    size: int
    signature: str

    def hook_metadata(self) -> dict[str, str]:
        return {
            TUS_ACCOUNT_METADATA: self.account,
            TUS_PATH_METADATA: self.relative_path,
            TUS_SIGNATURE_METADATA: self.signature,
        }


def authenticate_tus_account(config: JebIngressConfig, authorization: str | None) -> str:
    account, supplied_password = _basic_credentials(authorization)
    try:
        expected_password = config.tus_password(account)
    except JebIngressAuthenticationError as exc:
        raise JebIngressAuthenticationError("invalid Jeb ingress credentials") from exc
    if not secrets.compare_digest(supplied_password, expected_password):
        raise JebIngressAuthenticationError("invalid Jeb ingress credentials")
    return account


def prepare_tus_upload(
    config: JebIngressConfig,
    *,
    authorization: str | None,
    metadata: Mapping[str, object],
    size: object,
) -> PreparedTusUpload:
    account = authenticate_tus_account(config, authorization)
    parsed_size = _upload_size(size)
    relative_path = normalize_landing_path(metadata.get("path") or metadata.get("filename"))
    upload_id = uuid.uuid4().hex
    password = config.tus_password(account)
    return PreparedTusUpload(
        upload_id=upload_id,
        account=account,
        relative_path=relative_path,
        size=parsed_size,
        signature=_upload_signature(
            password=password,
            upload_id=upload_id,
            account=account,
            relative_path=relative_path,
            size=parsed_size,
        ),
    )


def publish_tus_upload(
    config: JebIngressConfig,
    *,
    upload: Mapping[str, object],
) -> Path:
    upload_id = str(upload.get("ID") or "")
    if not upload_id or any(ch not in "0123456789abcdef" for ch in upload_id):
        raise JebIngressError("Jeb TUS upload has an invalid ID")
    size = _upload_size(upload.get("Size"))
    if _upload_size(upload.get("Offset")) != size:
        raise JebIngressError("Jeb TUS upload is not complete")

    metadata = upload.get("MetaData")
    if not isinstance(metadata, Mapping):
        raise JebIngressError("Jeb TUS upload metadata is missing")
    account = str(metadata.get(TUS_ACCOUNT_METADATA) or "")
    relative_path = normalize_landing_path(metadata.get(TUS_PATH_METADATA))
    signature = str(metadata.get(TUS_SIGNATURE_METADATA) or "")
    expected_signature = _upload_signature(
        password=config.tus_password(account),
        upload_id=upload_id,
        account=account,
        relative_path=relative_path,
        size=size,
    )
    if not signature or not secrets.compare_digest(signature, expected_signature):
        raise JebIngressAuthenticationError("Jeb TUS upload metadata is not authentic")

    storage = upload.get("Storage")
    if not isinstance(storage, Mapping) or storage.get("Type") != "filestore":
        raise JebIngressError("Jeb TUS ingress requires filestore staging")
    expected_source = (config.tus_staging_dir / upload_id).resolve()
    expected_info = (config.tus_staging_dir / f"{upload_id}.info").resolve()
    source = Path(str(storage.get("Path") or "")).resolve()
    info = Path(str(storage.get("InfoPath") or "")).resolve()
    if source != expected_source or info != expected_info:
        raise JebIngressError("Jeb TUS upload escaped its staging directory")

    destination = _landing_destination(config, account=account, relative_path=relative_path)
    return JebLandingPublisher(config).publish(
        upload_id=upload_id,
        source=source,
        info=info,
        destination=destination,
        size=size,
    )


class JebLandingPublisher:
    def __init__(self, config: JebIngressConfig) -> None:
        self._landing_dir = config.landing_dir.resolve()
        self._staging_dir = config.tus_staging_dir.resolve()
        self._receipt_dir = self._staging_dir / ".published"

    def publish(
        self,
        *,
        upload_id: str,
        source: Path,
        info: Path,
        destination: Path,
        size: int,
    ) -> Path:
        source = source.resolve()
        info = info.resolve()
        destination = destination.resolve()
        self._require_within(source, self._staging_dir, label="source")
        self._require_within(info, self._staging_dir, label="info")
        self._require_within(destination, self._landing_dir, label="destination")
        receipt = self._receipt_dir / f"{upload_id}.json"
        relative_destination = destination.relative_to(self._landing_dir).as_posix()

        if receipt.exists():
            self._verify_receipt(
                receipt,
                upload_id=upload_id,
                relative_destination=relative_destination,
                destination=destination,
                size=size,
            )
            self._cleanup_staging(source, info)
            return destination

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

        self._write_receipt(
            receipt,
            {
                "upload_id": upload_id,
                "destination": relative_destination,
                "bytes": size,
            },
        )
        self._cleanup_staging(source, info)
        return destination

    @staticmethod
    def _require_within(path: Path, root: Path, *, label: str) -> None:
        if not path.is_relative_to(root):
            raise JebIngressError(f"Jeb ingress {label} escaped its configured root")

    @staticmethod
    def _cleanup_staging(source: Path, info: Path) -> None:
        source.unlink(missing_ok=True)
        info.unlink(missing_ok=True)

    @staticmethod
    def _verify_receipt(
        receipt: Path,
        *,
        upload_id: str,
        relative_destination: str,
        destination: Path,
        size: int,
    ) -> None:
        try:
            payload = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise JebIngressError("Jeb ingress publication receipt is invalid") from exc
        if payload != {
            "upload_id": upload_id,
            "destination": relative_destination,
            "bytes": size,
        }:
            raise JebIngressError("Jeb ingress publication receipt does not match the upload")
        if not destination.is_file() or destination.stat().st_size != size:
            raise JebIngressError("published Jeb landing file does not match its receipt")

    @staticmethod
    def _write_receipt(path: Path, payload: Mapping[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


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
    *,
    account: str,
    relative_path: str,
) -> Path:
    if account not in config.tus_accounts:
        raise JebIngressAuthenticationError("Jeb TUS account is not enabled")
    destination = (config.landing_dir / account / PurePosixPath(relative_path)).resolve()
    account_root = (config.landing_dir / account).resolve()
    if not destination.is_relative_to(account_root):
        raise JebIngressError("Jeb TUS upload escaped its account landing directory")
    return destination


def _basic_credentials(authorization: str | None) -> tuple[str, str]:
    scheme, _, encoded = str(authorization or "").partition(" ")
    if scheme.casefold() != "basic" or not encoded:
        raise JebIngressAuthenticationError("Jeb TUS ingress requires HTTP Basic credentials")
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise JebIngressAuthenticationError("invalid Jeb ingress credentials") from exc
    account, separator, password = decoded.partition(":")
    if not separator or not account or not password:
        raise JebIngressAuthenticationError("invalid Jeb ingress credentials")
    return account, password


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
    password: str,
    upload_id: str,
    account: str,
    relative_path: str,
    size: int,
) -> str:
    message = "\0".join((upload_id, account, relative_path, str(size))).encode("utf-8")
    return hmac.new(password.encode("utf-8"), message, hashlib.sha256).hexdigest()

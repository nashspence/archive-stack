"""Strict one-target AWS storage-adapter configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from riverhog_storage_adapter_protocol import normalize_object_path


def _bool(value: str, *, name: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _positive_int(value: str, *, name: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise ValueError(f"{name} must be positive")
    return parsed


@dataclass(frozen=True, slots=True)
class AwsStorageAdapterConfig:
    bucket: str
    region: str
    profile_id: str
    egress_accounting_id: str
    token_file: Path
    state_root: Path
    prefix: str = ""
    endpoint_url: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None
    session_token: str | None = None
    force_path_style: bool = False
    storage_class: str = "DEEP_ARCHIVE"
    read_mode: str = "restore_required"
    restore_tier: str = "Bulk"
    restore_hold_days: int = 1
    max_pool_connections: int = 32
    cloudfront_base_url: str | None = None
    cloudfront_public_key_id: str | None = None
    cloudfront_private_key_file: Path | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("bucket", self.bucket),
            ("region", self.region),
            ("profile_id", self.profile_id),
            ("egress_accounting_id", self.egress_accounting_id),
        ):
            if not value.strip():
                raise ValueError(f"AWS storage-adapter {name} must not be blank")
        if self.read_mode not in {"immediate", "restore_required"}:
            raise ValueError("AWS storage-adapter read mode is invalid")
        if self.restore_tier not in {"Bulk", "Standard"}:
            raise ValueError("AWS storage-adapter restore tier must be Bulk or Standard")
        if not self.storage_class.strip():
            raise ValueError("AWS storage-adapter storage class must not be blank")
        if self.max_pool_connections < 1:
            raise ValueError("AWS storage-adapter maximum pool connections must be positive")
        if (self.access_key_id is None) != (self.secret_access_key is None):
            raise ValueError("AWS explicit access-key configuration must be complete")
        normalized_prefix = self.prefix.strip("/")
        if normalized_prefix:
            normalized_prefix = normalize_object_path(normalized_prefix, allow_prefix=True)
        object.__setattr__(self, "prefix", normalized_prefix)
        if self.endpoint_url is not None:
            endpoint = urlsplit(self.endpoint_url)
            if (
                endpoint.scheme not in {"http", "https"}
                or not endpoint.hostname
                or endpoint.username is not None
                or endpoint.password is not None
                or endpoint.query
                or endpoint.fragment
            ):
                raise ValueError("AWS storage-adapter provider endpoint is invalid")
        cloudfront = (
            self.cloudfront_base_url,
            self.cloudfront_public_key_id,
            self.cloudfront_private_key_file,
        )
        if any(value is not None for value in cloudfront) and not all(
            value is not None for value in cloudfront
        ):
            raise ValueError("AWS CloudFront configuration must be complete")
        if self.cloudfront_base_url is not None:
            parsed = urlsplit(self.cloudfront_base_url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("AWS CloudFront base URL must be a private HTTPS delivery URL")

    @classmethod
    def from_env(cls) -> AwsStorageAdapterConfig:
        prefix = "RIVERHOG_AWS_STORAGE_ADAPTER_"
        required = {
            "bucket": os.getenv(prefix + "BUCKET", ""),
            "region": os.getenv(prefix + "REGION", ""),
            "profile_id": os.getenv(prefix + "PROFILE_ID", ""),
            "egress_accounting_id": os.getenv(prefix + "EGRESS_ACCOUNTING_ID", ""),
            "token_file": os.getenv(prefix + "TOKEN_FILE", ""),
            "state_root": os.getenv(prefix + "STATE_ROOT", ""),
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if missing:
            raise ValueError(
                "missing AWS storage-adapter configuration: " + ", ".join(sorted(missing))
            )
        private_key = os.getenv(prefix + "CLOUDFRONT_PRIVATE_KEY_FILE", "").strip()
        return cls(
            bucket=required["bucket"],
            region=required["region"],
            profile_id=required["profile_id"],
            egress_accounting_id=required["egress_accounting_id"],
            token_file=Path(required["token_file"]),
            state_root=Path(required["state_root"]),
            prefix=os.getenv(prefix + "PREFIX", ""),
            endpoint_url=os.getenv(prefix + "ENDPOINT_URL", "").strip() or None,
            access_key_id=os.getenv(prefix + "ACCESS_KEY_ID", "").strip() or None,
            secret_access_key=os.getenv(prefix + "SECRET_ACCESS_KEY", "").strip() or None,
            session_token=os.getenv(prefix + "SESSION_TOKEN", "").strip() or None,
            force_path_style=_bool(
                os.getenv(prefix + "FORCE_PATH_STYLE", "false"),
                name=prefix + "FORCE_PATH_STYLE",
            ),
            storage_class=os.getenv(prefix + "STORAGE_CLASS", "DEEP_ARCHIVE").strip(),
            read_mode=os.getenv(prefix + "READ_MODE", "restore_required").strip(),
            restore_tier=os.getenv(prefix + "RESTORE_TIER", "Bulk").strip().title(),
            restore_hold_days=_positive_int(
                os.getenv(prefix + "RESTORE_HOLD_DAYS", "1"),
                name=prefix + "RESTORE_HOLD_DAYS",
            ),
            max_pool_connections=_positive_int(
                os.getenv(prefix + "MAX_POOL_CONNECTIONS", "32"),
                name=prefix + "MAX_POOL_CONNECTIONS",
            ),
            cloudfront_base_url=(
                os.getenv(prefix + "CLOUDFRONT_BASE_URL", "").strip().rstrip("/") or None
            ),
            cloudfront_public_key_id=(
                os.getenv(prefix + "CLOUDFRONT_PUBLIC_KEY_ID", "").strip() or None
            ),
            cloudfront_private_key_file=Path(private_key) if private_key else None,
        )


__all__ = ["AwsStorageAdapterConfig"]

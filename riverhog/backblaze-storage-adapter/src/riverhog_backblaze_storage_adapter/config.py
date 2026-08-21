"""Strict one-target Backblaze storage-adapter configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from riverhog_storage_adapter_protocol import normalize_object_path


def _positive_int(value: str, *, name: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise ValueError(f"{name} must be positive")
    return parsed


@dataclass(frozen=True, slots=True)
class BackblazeStorageAdapterConfig:
    endpoint_url: str
    region: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    profile_id: str
    egress_accounting_id: str
    token_file: Path
    state_root: Path
    prefix: str = ""
    max_pool_connections: int = 32

    def __post_init__(self) -> None:
        for name, value in (
            ("endpoint URL", self.endpoint_url),
            ("region", self.region),
            ("bucket", self.bucket),
            ("access key ID", self.access_key_id),
            ("secret access key", self.secret_access_key),
            ("profile ID", self.profile_id),
            ("egress-accounting ID", self.egress_accounting_id),
        ):
            if not value.strip():
                raise ValueError(f"Backblaze storage-adapter {name} must not be blank")
        endpoint = urlsplit(self.endpoint_url)
        if (
            endpoint.scheme != "https"
            or not endpoint.hostname
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("Backblaze storage-adapter provider endpoint must use HTTPS")
        normalized_prefix = self.prefix.strip("/")
        if normalized_prefix:
            normalized_prefix = normalize_object_path(normalized_prefix, allow_prefix=True)
        object.__setattr__(
            self,
            "prefix",
            normalized_prefix,
        )
        if self.max_pool_connections < 1:
            raise ValueError("Backblaze maximum pool connections must be positive")

    @classmethod
    def from_env(cls) -> BackblazeStorageAdapterConfig:
        prefix = "RIVERHOG_BACKBLAZE_STORAGE_ADAPTER_"
        values = {
            "endpoint_url": os.getenv(prefix + "ENDPOINT_URL", ""),
            "region": os.getenv(prefix + "REGION", ""),
            "bucket": os.getenv(prefix + "BUCKET", ""),
            "access_key_id": os.getenv(prefix + "ACCESS_KEY_ID", ""),
            "secret_access_key": os.getenv(prefix + "SECRET_ACCESS_KEY", ""),
            "profile_id": os.getenv(prefix + "PROFILE_ID", ""),
            "egress_accounting_id": os.getenv(prefix + "EGRESS_ACCOUNTING_ID", ""),
            "token_file": os.getenv(prefix + "TOKEN_FILE", ""),
            "state_root": os.getenv(prefix + "STATE_ROOT", ""),
        }
        missing = [name for name, value in values.items() if not value.strip()]
        if missing:
            raise ValueError(
                "missing Backblaze storage-adapter configuration: " + ", ".join(sorted(missing))
            )
        return cls(
            endpoint_url=values["endpoint_url"].rstrip("/"),
            region=values["region"],
            bucket=values["bucket"],
            access_key_id=values["access_key_id"],
            secret_access_key=values["secret_access_key"],
            profile_id=values["profile_id"],
            egress_accounting_id=values["egress_accounting_id"],
            token_file=Path(values["token_file"]),
            state_root=Path(values["state_root"]),
            prefix=os.getenv(prefix + "PREFIX", ""),
            max_pool_connections=_positive_int(
                os.getenv(prefix + "MAX_POOL_CONNECTIONS", "32"),
                name=prefix + "MAX_POOL_CONNECTIONS",
            ),
        )


__all__ = ["BackblazeStorageAdapterConfig"]

"""Strict one-target local Garage storage-adapter configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from riverhog_storage_adapter_protocol import normalize_object_path


@dataclass(frozen=True, slots=True)
class GarageStorageAdapterConfig:
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
                raise ValueError(f"Garage storage-adapter {name} must not be blank")
        endpoint = urlsplit(self.endpoint_url)
        if (
            endpoint.scheme not in {"http", "https"}
            or not endpoint.hostname
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("Garage storage-adapter provider endpoint is invalid")
        normalized_prefix = self.prefix.strip("/")
        if normalized_prefix:
            normalized_prefix = normalize_object_path(normalized_prefix, allow_prefix=True)
        object.__setattr__(self, "prefix", normalized_prefix)
        if self.max_pool_connections < 1:
            raise ValueError("Garage maximum pool connections must be positive")

    @classmethod
    def from_env(cls) -> GarageStorageAdapterConfig:
        prefix = "RIVERHOG_GARAGE_STORAGE_ADAPTER_"
        defaults = {
            "ENDPOINT_URL": "http://garage:3900",
            "REGION": "garage",
            "BUCKET": "riverhog-archive",
            "ACCESS_KEY_ID": "GK000000000000000000000002",
            "SECRET_ACCESS_KEY": "2222222222222222222222222222222222222222222222222222222222222222",
            "PROFILE_ID": "riverhog.garage-development/v1",
            "EGRESS_ACCOUNTING_ID": "riverhog-garage-development",
            "TOKEN_FILE": "/run/secrets/riverhog-storage-adapter-token",
            "STATE_ROOT": "/var/lib/riverhog-garage-storage-adapter",
        }
        values = {
            name.casefold(): os.getenv(prefix + name, default) for name, default in defaults.items()
        }
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
            max_pool_connections=int(os.getenv(prefix + "MAX_POOL_CONNECTIONS", "32")),
        )


__all__ = ["GarageStorageAdapterConfig"]

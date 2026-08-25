"""Test-only Garage binding for the provider-neutral storage-adapter contract."""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any, Literal

import uvicorn
from riverhog_storage_adapter_asgi_support import create_storage_adapter_app
from riverhog_storage_adapter_protocol import ReadReadiness, ReadReady
from riverhog_storage_adapter_s3_support import (
    S3ClientConfig,
    S3StorageAdapter,
    S3StorageAdapterConfig,
    create_s3_client,
)

_PREFIX = "RIVERHOG_GARAGE_STORAGE_ADAPTER_"


class _AlwaysReadyPreparation:
    """Exercise restore-required Riverhog behavior without imitating a cloud provider."""

    def prepare(self, **_: Any) -> ReadReadiness:
        return ReadReady()

    def status(self, **_: Any) -> ReadReadiness:
        return ReadReady()

    def cleanup(self, **_: Any) -> None:
        return None


def main(argv: Sequence[str] | None = None) -> int:
    _ = argv
    read_mode = _read_mode(_value("READ_MODE", "immediate"))
    client = create_s3_client(
        S3ClientConfig(
            endpoint_url=_value("ENDPOINT_URL", "http://garage:3900"),
            region=_value("REGION", "garage"),
            access_key_id=_required("ACCESS_KEY_ID"),
            secret_access_key=_required("SECRET_ACCESS_KEY"),
            force_path_style=True,
        )
    )
    adapter = S3StorageAdapter(
        client,
        S3StorageAdapterConfig(
            implementation_id="riverhog.test-garage/v1",
            implementation_version="1.0.0",
            bucket=_required("BUCKET"),
            read_mode=read_mode,
        ),
        read_preparation=(_AlwaysReadyPreparation() if read_mode == "restore_required" else None),
    )
    app = create_storage_adapter_app(
        service="riverhog-test-garage-storage-adapter",
        token=_required("TOKEN"),
        adapter=adapter,
        readiness=lambda: client.head_bucket(Bucket=_required("BUCKET")),
    )
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(_value("PORT", "8080")),
    )
    return 0


def _required(name: str) -> str:
    value = os.getenv(f"{_PREFIX}{name}", "").strip()
    if not value:
        raise ValueError(f"{_PREFIX}{name} must be nonempty")
    return value


def _value(name: str, default: str) -> str:
    return os.getenv(f"{_PREFIX}{name}", default).strip() or default


def _read_mode(value: str) -> Literal["immediate", "restore_required"]:
    if value not in {"immediate", "restore_required"}:
        raise ValueError(f"{_PREFIX}READ_MODE must be immediate or restore_required")
    return "restore_required" if value == "restore_required" else "immediate"


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]

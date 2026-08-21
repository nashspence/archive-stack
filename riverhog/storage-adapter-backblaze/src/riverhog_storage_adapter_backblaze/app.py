"""Runtime configuration for the narrowly scoped first-party Backblaze adapter."""

from __future__ import annotations

import argparse
import contextlib
import importlib.metadata
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

import uvicorn
from riverhog_storage_adapter_asgi_support import create_storage_adapter_app
from riverhog_storage_adapter_s3_support import (
    S3ClientConfig,
    S3StorageAdapter,
    S3StorageAdapterConfig,
    S3TransportTuning,
    create_s3_client,
)

SERVICE = "riverhog-storage-adapter-backblaze"
_PREFIX = "RIVERHOG_BACKBLAZE_STORAGE_ADAPTER_"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=SERVICE)
    parser.add_argument("--version", action="version", version=importlib.metadata.version(SERVICE))
    parser.add_argument("--host", default=os.getenv(f"{_PREFIX}HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv(f"{_PREFIX}PORT", "8080")))
    args = parser.parse_args(argv)
    token = _secret("TOKEN")
    bucket = _required("BUCKET")
    client = create_s3_client(
        S3ClientConfig(
            endpoint_url=_required("ENDPOINT_URL"),
            region=_required("REGION"),
            access_key_id=_secret("ACCESS_KEY_ID"),
            secret_access_key=_secret("SECRET_ACCESS_KEY"),
            force_path_style=_bool("FORCE_PATH_STYLE", False),
        ),
        tuning=S3TransportTuning(
            max_pool_connections=_int("MAX_POOL_CONNECTIONS", 32),
            connect_timeout_seconds=_float("CONNECT_TIMEOUT_SECONDS", 10.0),
            read_timeout_seconds=_float("READ_TIMEOUT_SECONDS", 300.0),
            max_attempts=_int("MAX_ATTEMPTS", 8),
            retry_mode=_retry_mode(_optional("RETRY_MODE") or "standard"),
            tcp_keepalive=_bool("TCP_KEEPALIVE", True),
        ),
    )
    adapter = S3StorageAdapter(
        client,
        S3StorageAdapterConfig(
            implementation_id="riverhog.backblaze/v1",
            implementation_version=importlib.metadata.version(SERVICE),
            bucket=bucket,
            root_prefix=_optional("ROOT_PREFIX") or "",
            read_mode="immediate",
            read_chunk_bytes=_int("READ_CHUNK_BYTES", 8 * 1024 * 1024),
        ),
    )

    def readiness() -> None:
        client.head_bucket(Bucket=bucket)

    uvicorn.run(
        create_storage_adapter_app(
            service=SERVICE,
            token=token,
            adapter=adapter,
            readiness=readiness,
        ),
        host=args.host,
        port=args.port,
    )
    return 0


def _secret(name: str) -> str:
    direct_name = f"{_PREFIX}{name}"
    file_name = f"{direct_name}_FILE"
    direct = os.getenv(direct_name)
    path = os.getenv(file_name)
    if bool(direct) == bool(path):
        raise ValueError(f"set exactly one of {direct_name} or {file_name}")
    value = direct if direct is not None else Path(str(path)).read_text(encoding="utf-8")
    if not value.strip():
        raise ValueError(f"{direct_name} must be nonempty")
    with contextlib.suppress(KeyError):
        os.environ.pop(direct_name)
    return value.strip()


def _required(name: str) -> str:
    variable = f"{_PREFIX}{name}"
    value = os.getenv(variable, "").strip()
    if not value:
        raise ValueError(f"{variable} must be nonempty")
    return value


def _optional(name: str) -> str | None:
    value = os.getenv(f"{_PREFIX}{name}", "").strip()
    return value or None


def _int(name: str, default: int) -> int:
    return int(_optional(name) or str(default))


def _float(name: str, default: float) -> float:
    return float(_optional(name) or str(default))


def _bool(name: str, default: bool) -> bool:
    value = _optional(name)
    if value is None:
        return default
    if value.casefold() in {"1", "true", "yes", "on"}:
        return True
    if value.casefold() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{_PREFIX}{name} must be boolean")


def _retry_mode(value: str) -> Literal["standard", "adaptive"]:
    if value not in {"standard", "adaptive"}:
        raise ValueError("Backblaze adapter retry mode must be standard or adaptive")
    return "adaptive" if value == "adaptive" else "standard"


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["SERVICE", "main"]

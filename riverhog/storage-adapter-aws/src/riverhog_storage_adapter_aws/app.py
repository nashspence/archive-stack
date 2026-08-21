"""Runtime configuration for the narrowly scoped first-party AWS adapter."""

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

from riverhog_storage_adapter_aws.provider import (
    AwsCloudFrontConfig,
    AwsCloudFrontObjectReader,
    AwsDeepArchiveReadPreparation,
)

SERVICE = "riverhog-storage-adapter-aws"
_PREFIX = "RIVERHOG_AWS_STORAGE_ADAPTER_"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=SERVICE)
    parser.add_argument("--version", action="version", version=importlib.metadata.version(SERVICE))
    parser.add_argument("--host", default=os.getenv(f"{_PREFIX}HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv(f"{_PREFIX}PORT", "8080")))
    args = parser.parse_args(argv)
    token = _secret("TOKEN")
    client = create_s3_client(
        S3ClientConfig(
            endpoint_url=_optional("ENDPOINT_URL"),
            region=_required("REGION"),
            access_key_id=_secret("ACCESS_KEY_ID"),
            secret_access_key=_secret("SECRET_ACCESS_KEY"),
            session_token=_optional_secret("SESSION_TOKEN"),
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
    read_mode = _read_mode(_optional("READ_MODE") or "restore_required")
    preparation = (
        AwsDeepArchiveReadPreparation(
            tier=_optional("RESTORE_TIER") or "Bulk",
            days=_int("RESTORE_DAYS", 3),
        )
        if read_mode == "restore_required"
        else None
    )
    cloudfront = _cloudfront_reader()
    adapter = S3StorageAdapter(
        client,
        S3StorageAdapterConfig(
            implementation_id="riverhog.aws/v1",
            implementation_version=importlib.metadata.version(SERVICE),
            bucket=_required("BUCKET"),
            root_prefix=_optional("ROOT_PREFIX") or "",
            read_mode=read_mode,
            archive_storage_class=_optional("ARCHIVE_STORAGE_CLASS") or "DEEP_ARCHIVE",
            immediate_storage_class=_optional("IMMEDIATE_STORAGE_CLASS"),
            read_chunk_bytes=_int("READ_CHUNK_BYTES", 8 * 1024 * 1024),
        ),
        read_preparation=preparation,
        object_reader=cloudfront,
    )

    def readiness() -> None:
        client.head_bucket(Bucket=_required("BUCKET"))

    app = create_storage_adapter_app(
        service=SERVICE,
        token=token,
        adapter=adapter,
        readiness=readiness,
    )
    try:
        uvicorn.run(app, host=args.host, port=args.port)
    finally:
        if cloudfront is not None:
            cloudfront.close()
    return 0


def _cloudfront_reader() -> AwsCloudFrontObjectReader | None:
    values = {
        "base_url": _optional("CLOUDFRONT_BASE_URL"),
        "public_key_id": _optional("CLOUDFRONT_PUBLIC_KEY_ID"),
        "private_key_path": _optional("CLOUDFRONT_PRIVATE_KEY_PATH"),
    }
    configured = [name for name, value in values.items() if value is not None]
    if not configured:
        return None
    if len(configured) != len(values):
        raise ValueError("AWS adapter CloudFront configuration must be complete")
    return AwsCloudFrontObjectReader(
        AwsCloudFrontConfig(
            base_url=str(values["base_url"]),
            public_key_id=str(values["public_key_id"]),
            private_key_path=Path(str(values["private_key_path"])),
        )
    )


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


def _optional_secret(name: str) -> str | None:
    direct_name = f"{_PREFIX}{name}"
    file_name = f"{direct_name}_FILE"
    direct = os.getenv(direct_name)
    path = os.getenv(file_name)
    if direct is not None and path is not None:
        raise ValueError(f"set at most one of {direct_name} or {file_name}")
    if direct is None and path is None:
        return None
    value = direct if direct is not None else Path(str(path)).read_text(encoding="utf-8")
    if not value.strip():
        raise ValueError(f"{direct_name} must be nonempty when configured")
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
        raise ValueError("AWS adapter retry mode must be standard or adaptive")
    return "adaptive" if value == "adaptive" else "standard"


def _read_mode(value: str) -> Literal["immediate", "restore_required"]:
    if value not in {"immediate", "restore_required"}:
        raise ValueError("AWS adapter read mode must be immediate or restore_required")
    return "restore_required" if value == "restore_required" else "immediate"


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["SERVICE", "main"]

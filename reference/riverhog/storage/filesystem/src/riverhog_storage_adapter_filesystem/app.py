"""Executable ASGI service for the Linux filesystem storage adapter."""

from __future__ import annotations

import argparse
import contextlib
import importlib.metadata
import os
import re
from collections.abc import Sequence
from pathlib import Path

import uvicorn
from riverhog_storage_adapter_asgi_support import create_storage_adapter_app

from riverhog_storage_adapter_filesystem.adapter import (
    FilesystemStorageAdapter,
    FilesystemStorageAdapterConfig,
)

SERVICE = "riverhog-storage-adapter-filesystem"
_PREFIX = "RIVERHOG_FILESYSTEM_STORAGE_ADAPTER_"
_BYTE_SIZE = re.compile(r"^(?P<count>\d+)(?P<unit>b|kb|mb|gb|tb|kib|mib|gib|tib)?$", re.I)
_BYTE_FACTORS = {
    "": 1,
    "b": 1,
    "kb": 1_000,
    "mb": 1_000_000,
    "gb": 1_000_000_000,
    "tb": 1_000_000_000_000,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=SERVICE)
    parser.add_argument("--version", action="version", version=_version())
    parser.add_argument("--host", default=os.getenv(f"{_PREFIX}HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv(f"{_PREFIX}PORT", "8080")))
    args = parser.parse_args(argv)

    os.umask(0o077)
    token = _secret("TOKEN")
    adapter = FilesystemStorageAdapter(config_from_environment())
    app = create_storage_adapter_app(
        service=SERVICE,
        token=token,
        adapter=adapter,
        readiness=adapter.readiness,
    )
    try:
        uvicorn.run(app, host=args.host, port=args.port)
    finally:
        adapter.close()
    return 0


def config_from_environment() -> FilesystemStorageAdapterConfig:
    """Build exact private configuration from the process environment."""

    root = Path(_required("ROOT"))
    if not root.is_absolute() or root == Path("/"):
        raise ValueError(f"{_PREFIX}ROOT must be an absolute non-root directory")
    return FilesystemStorageAdapterConfig(
        root=root,
        implementation_version=_version(),
        segment_bytes=_parse_bytes(
            _optional("SEGMENT_BYTES") or "64MiB",
            f"{_PREFIX}SEGMENT_BYTES",
            minimum=64 * 1024,
        ),
        read_chunk_bytes=_parse_bytes(
            _optional("READ_CHUNK_BYTES") or "8MiB",
            f"{_PREFIX}READ_CHUNK_BYTES",
            minimum=64 * 1024,
        ),
        minimum_free_bytes=_parse_bytes(
            _optional("MINIMUM_FREE_BYTES") or "256MiB",
            f"{_PREFIX}MINIMUM_FREE_BYTES",
            minimum=0,
        ),
    )


def _version() -> str:
    try:
        return importlib.metadata.version(SERVICE)
    except importlib.metadata.PackageNotFoundError:
        return "0.1.0"


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


def _parse_bytes(raw: str, name: str, *, minimum: int) -> int:
    candidate = raw.strip().casefold().replace(" ", "")
    match = _BYTE_SIZE.fullmatch(candidate)
    if match is None:
        raise ValueError(f"{name} must be a byte size such as 64MiB")
    value = int(match.group("count")) * _BYTE_FACTORS[match.group("unit") or ""]
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum} bytes")
    return value


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["SERVICE", "config_from_environment", "main"]

"""Local/test Garage storage-adapter process entrypoint."""

from __future__ import annotations

import argparse
import importlib.metadata
import os
from collections.abc import Sequence

import uvicorn
from riverhog_storage_adapter_support import (
    StorageAdapterService,
    UploadJournal,
    create_storage_adapter_app,
)

from riverhog_garage_storage_adapter.config import GarageStorageAdapterConfig
from riverhog_garage_storage_adapter.driver import GarageStorageDriver

SERVICE = "riverhog-garage-storage-adapter"


def _version() -> str:
    try:
        return importlib.metadata.version(SERVICE)
    except importlib.metadata.PackageNotFoundError:
        return "development"


def build_service(config: GarageStorageAdapterConfig) -> StorageAdapterService:
    return StorageAdapterService(
        driver=GarageStorageDriver(
            config,
            implementation_version=_version(),
            source_revision=os.getenv(
                "RIVERHOG_GARAGE_STORAGE_ADAPTER_SOURCE_REVISION",
                "unknown",
            ),
        ),
        journal=UploadJournal(config.state_root / "storage-adapter.sqlite3"),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=SERVICE)
    parser.add_argument("--version", action="version", version=_version())
    parser.add_argument(
        "--host",
        default=os.getenv("RIVERHOG_GARAGE_STORAGE_ADAPTER_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("RIVERHOG_GARAGE_STORAGE_ADAPTER_PORT", "8080")),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = GarageStorageAdapterConfig.from_env()
    token = config.token_file.read_text(encoding="utf-8").strip()
    app = create_storage_adapter_app(
        service_name=SERVICE,
        token=token,
        service=build_service(config),
    )
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["SERVICE", "build_service", "main"]

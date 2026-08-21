"""Adapter-local Garage configured-root recovery export command."""

from __future__ import annotations

import importlib.metadata
import os
from collections.abc import Sequence

from riverhog_storage_adapter_support import recovery_export_main

from riverhog_garage_storage_adapter.config import GarageStorageAdapterConfig
from riverhog_garage_storage_adapter.driver import GarageStorageDriver

SERVICE = "riverhog-garage-storage-adapter"


def main(argv: Sequence[str] | None = None) -> int:
    try:
        version = importlib.metadata.version(SERVICE)
    except importlib.metadata.PackageNotFoundError:
        version = "development"

    def source() -> GarageStorageDriver:
        return GarageStorageDriver(
            GarageStorageAdapterConfig.from_env(),
            implementation_version=version,
            source_revision=os.getenv(
                "RIVERHOG_GARAGE_STORAGE_ADAPTER_SOURCE_REVISION",
                "unknown",
            ),
        )

    return recovery_export_main(
        source,
        prog="riverhog-garage-storage-adapter-export",
        version=version,
        argv=argv,
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]

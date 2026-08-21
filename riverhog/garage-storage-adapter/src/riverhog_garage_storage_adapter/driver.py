"""Garage-owned local conformance implementation of the storage-adapter seam."""

from __future__ import annotations

from typing import Any

from riverhog_storage_adapter_protocol import (
    StorageAdapterDescriptor,
    StorageAdapterDescriptorPayload,
    StorageProfile,
    StorageProfilePayload,
)
from riverhog_storage_adapter_s3_support import S3CompatibleStorageDriver, make_s3_client

from riverhog_garage_storage_adapter.config import GarageStorageAdapterConfig


class GarageStorageDriver(S3CompatibleStorageDriver):
    """One disposable Garage target used only by checked local conformance rails."""

    def __init__(
        self,
        config: GarageStorageAdapterConfig,
        *,
        implementation_version: str,
        source_revision: str,
        client: Any | None = None,
    ) -> None:
        profile = StorageProfile.seal(
            StorageProfilePayload(
                profile_id=config.profile_id,
                read_mode="immediate",
                egress_accounting_id=config.egress_accounting_id,
            )
        )
        descriptor = StorageAdapterDescriptor.seal(
            StorageAdapterDescriptorPayload(
                implementation_id="riverhog.garage-storage-adapter/v1",
                implementation_version=implementation_version,
                source_revision=source_revision,
                profile=profile,
                minimum_nonfinal_part_bytes=5 * 1024**2,
                maximum_part_bytes=5 * 1024**3,
                maximum_part_count=10_000,
            )
        )
        super().__init__(
            target=config,
            descriptor=descriptor,
            client=client
            or make_s3_client(
                endpoint_url=config.endpoint_url,
                region=config.region,
                access_key_id=config.access_key_id,
                secret_access_key=config.secret_access_key,
                force_path_style=True,
                max_pool_connections=config.max_pool_connections,
            ),
            provider_label="Garage",
        )


__all__ = ["GarageStorageDriver"]

"""Backblaze-owned implementation of the provider-neutral storage-adapter seam."""

from __future__ import annotations

from typing import Any

from riverhog_storage_adapter_protocol import (
    StorageAdapterDescriptor,
    StorageAdapterDescriptorPayload,
    StorageProfile,
    StorageProfilePayload,
)
from riverhog_storage_adapter_s3_support import S3CompatibleStorageDriver, make_s3_client

from riverhog_backblaze_storage_adapter.config import BackblazeStorageAdapterConfig


class BackblazeStorageDriver(S3CompatibleStorageDriver):
    """One scoped B2 bucket target; its S3 compatibility stays behind this adapter."""

    def __init__(
        self,
        config: BackblazeStorageAdapterConfig,
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
                implementation_id="riverhog.backblaze-storage-adapter/v1",
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
                max_pool_connections=config.max_pool_connections,
            ),
            provider_label="Backblaze",
        )


__all__ = ["BackblazeStorageDriver"]

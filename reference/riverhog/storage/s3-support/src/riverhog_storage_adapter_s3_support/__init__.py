"""Common implementation support for scoped S3-backed storage adapters."""

from riverhog_storage_adapter_s3_support.adapter import (
    S3ReadPreparation,
    S3StorageAdapter,
    S3StorageAdapterConfig,
)
from riverhog_storage_adapter_s3_support.client import (
    S3ClientConfig,
    S3TransportTuning,
    create_s3_client,
)

__all__ = [
    "S3ClientConfig",
    "S3ReadPreparation",
    "S3StorageAdapter",
    "S3StorageAdapterConfig",
    "S3TransportTuning",
    "create_s3_client",
]

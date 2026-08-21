"""Optional helpers for implementing an S3-compatible Riverhog storage adapter."""

from riverhog_storage_adapter_s3_support.driver import (
    S3CompatibleStorageDriver,
    S3Target,
    make_s3_client,
)

__all__ = ["S3CompatibleStorageDriver", "S3Target", "make_s3_client"]

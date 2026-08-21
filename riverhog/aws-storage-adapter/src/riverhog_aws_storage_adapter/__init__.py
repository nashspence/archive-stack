"""Scoped first-party AWS storage adapter."""

from riverhog_aws_storage_adapter.config import AwsStorageAdapterConfig
from riverhog_aws_storage_adapter.driver import AwsStorageDriver

__all__ = ["AwsStorageAdapterConfig", "AwsStorageDriver"]

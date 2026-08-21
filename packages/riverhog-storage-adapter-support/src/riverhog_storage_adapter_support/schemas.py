"""Generated JSON Schema bundle for the storage-adapter contract."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
from collections.abc import Sequence

from riverhog_storage_adapter_protocol import (
    AbortIncompleteUploadsRequest,
    CompleteUploadRequest,
    MaintenanceResult,
    ObjectDeleteRequest,
    ObjectLocator,
    ObjectReceipt,
    PrefixDeleteRequest,
    ReadRequest,
    ReadStatus,
    StorageAdapterDescriptor,
    StorageAdapterDescriptorPayload,
    StorageAdapterError,
    StorageProfile,
    StorageProfilePayload,
    UploadDeclaration,
    UploadDeclarationPayload,
    UploadPartReceipt,
    UploadStatus,
    WriteCondition,
)

STORAGE_ADAPTER_SCHEMA_BUNDLE_FORMAT = "riverhog-storage-adapter-schemas/v1"
_DISTRIBUTION = "riverhog-storage-adapter-support"
_MODELS = (
    StorageProfilePayload,
    StorageProfile,
    StorageAdapterDescriptorPayload,
    StorageAdapterDescriptor,
    WriteCondition,
    UploadDeclarationPayload,
    UploadDeclaration,
    UploadPartReceipt,
    CompleteUploadRequest,
    UploadStatus,
    ObjectLocator,
    ObjectDeleteRequest,
    ObjectReceipt,
    PrefixDeleteRequest,
    ReadRequest,
    ReadStatus,
    AbortIncompleteUploadsRequest,
    MaintenanceResult,
    StorageAdapterError,
)


def storage_adapter_schema_bundle() -> dict[str, object]:
    return {
        "format": STORAGE_ADAPTER_SCHEMA_BUNDLE_FORMAT,
        "schemas": {
            model.__name__: model.model_json_schema(mode="validation") for model in _MODELS
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="riverhog-storage-adapter-schemas",
        description="Print the Riverhog storage-adapter v1 JSON Schema bundle.",
    )
    try:
        version = importlib.metadata.version(_DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        version = "development"
    parser.add_argument("--version", action="version", version=version)
    parser.parse_args(argv)
    print(json.dumps(storage_adapter_schema_bundle(), indent=2, sort_keys=True))
    return 0


__all__ = [
    "STORAGE_ADAPTER_SCHEMA_BUNDLE_FORMAT",
    "main",
    "storage_adapter_schema_bundle",
]

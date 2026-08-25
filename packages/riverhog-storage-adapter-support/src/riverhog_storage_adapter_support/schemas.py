"""Machine-readable v1 storage-adapter schema and HTTP binding inventory."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from riverhog_storage_adapter_protocol import (
    STORAGE_ADAPTER_PROTOCOL,
    AbortIncompleteWritesRequest,
    AdapterDescriptor,
    CompletedObjectReceipt,
    CompletedWriteLookupRequest,
    DeleteObjectRequest,
    DeletePrefixRequest,
    ImmutableObjectReceipt,
    MaintenanceResult,
    ObjectHeadRequest,
    ObjectLocator,
    ObjectMetadataReceipt,
    ObjectReadRequest,
    ReadPreparationRequest,
    ReadStatus,
    SmallObjectWriteRequest,
    StorageAdapterError,
    WriteCompleteRequest,
    WriteSegmentReceipt,
    WriteSegmentRequest,
    WriteSession,
    WriteStartRequest,
)

STORAGE_ADAPTER_SCHEMA_BUNDLE_FORMAT = "riverhog-storage-adapter-schema-bundle/v1"

_SCHEMA_MODELS: tuple[type[BaseModel], ...] = (
    AbortIncompleteWritesRequest,
    AdapterDescriptor,
    CompletedObjectReceipt,
    DeleteObjectRequest,
    DeletePrefixRequest,
    ImmutableObjectReceipt,
    MaintenanceResult,
    WriteCompleteRequest,
    WriteStartRequest,
    CompletedWriteLookupRequest,
    WriteSegmentReceipt,
    WriteSegmentRequest,
    WriteSession,
    ObjectLocator,
    ObjectHeadRequest,
    ObjectMetadataReceipt,
    ObjectReadRequest,
    ReadPreparationRequest,
    ReadStatus,
    SmallObjectWriteRequest,
    StorageAdapterError,
)


def storage_adapter_schema_bundle() -> dict[str, Any]:
    return {
        "format": STORAGE_ADAPTER_SCHEMA_BUNDLE_FORMAT,
        "protocol": STORAGE_ADAPTER_PROTOCOL,
        "compatibility": {
            "unknown_fields": "reject",
            "provider_ontology": "private",
        },
        "http_binding": {
            "GET /v1/adapter": "AdapterDescriptor",
            "POST /v1/writes/begin": "WriteStartRequest -> WriteSession",
            "POST /v1/writes/segment": (
                "framed WriteSegmentRequest + bytes -> WriteSegmentReceipt"
            ),
            "POST /v1/writes/segments": "WriteSession -> WriteSegmentReceipt[]",
            "POST /v1/writes/complete": ("WriteCompleteRequest -> CompletedObjectReceipt"),
            "POST /v1/writes/completed": (
                "CompletedWriteLookupRequest -> CompletedObjectReceipt|404"
            ),
            "POST /v1/writes/abort": "WriteSession -> 204",
            "POST /v1/objects/put": (
                "framed SmallObjectWriteRequest + bytes -> ImmutableObjectReceipt"
            ),
            "POST /v1/objects/head": "ObjectHeadRequest -> ObjectMetadataReceipt|404",
            "POST /v1/objects/read": "ObjectReadRequest -> byte stream",
            "POST /v1/objects/delete": "DeleteObjectRequest -> 204",
            "POST /v1/objects/delete-prefix": "DeletePrefixRequest -> MaintenanceResult",
            "POST /v1/reads/prepare": "ReadPreparationRequest -> ReadStatus",
            "POST /v1/reads/status": "ReadPreparationRequest -> ReadStatus",
            "POST /v1/reads/cleanup": "ReadPreparationRequest -> 204",
            "POST /v1/maintenance/abort-incomplete-writes": (
                "AbortIncompleteWritesRequest -> MaintenanceResult"
            ),
        },
        "schemas": {
            model.__name__: model.model_json_schema(mode="validation") for model in _SCHEMA_MODELS
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="riverhog-storage-adapter-schemas",
        description="Emit the Riverhog storage-adapter v1 schema bundle.",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compact", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    bundle = storage_adapter_schema_bundle()
    content = (
        json.dumps(bundle, sort_keys=True, separators=(",", ":"))
        if args.compact
        else json.dumps(bundle, indent=2, sort_keys=True)
    ) + "\n"
    if args.output is None:
        print(content, end="")
    else:
        args.output.write_text(content, encoding="utf-8")
    return 0


__all__ = [
    "STORAGE_ADAPTER_SCHEMA_BUNDLE_FORMAT",
    "main",
    "storage_adapter_schema_bundle",
]

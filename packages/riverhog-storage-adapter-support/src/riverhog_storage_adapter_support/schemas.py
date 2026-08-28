"""Machine-readable v1 storage-adapter schema and HTTP binding inventory."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from http_api_contracts import http_operation_inventory, structural_model_catalog
from riverhog_storage_adapter_protocol import STORAGE_ADAPTER_PROTOCOL, StorageAdapterError

from riverhog_storage_adapter_support.http_binding import STORAGE_ADAPTER_HTTP_OPERATIONS

STORAGE_ADAPTER_SCHEMA_BUNDLE_FORMAT = "riverhog-storage-adapter-schema-bundle/v1"


def storage_adapter_schema_bundle() -> dict[str, Any]:
    return {
        "format": STORAGE_ADAPTER_SCHEMA_BUNDLE_FORMAT,
        "protocol": STORAGE_ADAPTER_PROTOCOL,
        "compatibility": {
            "unknown_fields": "reject",
            "provider_ontology": "private",
        },
        "authorities": {
            "structural_models": "schemas",
            "http_operations": "http_binding.operations",
            "semantic_acceptance": "semantic_acceptance",
        },
        "http_binding": {
            "operations": http_operation_inventory(STORAGE_ADAPTER_HTTP_OPERATIONS),
        },
        "semantic_acceptance": {
            "kind": "session-and-object-relations",
            "conformance": "riverhog-storage-adapter-conformance-result/v1",
        },
        "schemas": structural_model_catalog(
            STORAGE_ADAPTER_HTTP_OPERATIONS,
            additional_models=(StorageAdapterError,),
        ),
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

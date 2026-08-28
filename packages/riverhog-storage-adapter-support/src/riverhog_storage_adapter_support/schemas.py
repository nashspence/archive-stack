"""Machine-readable v1 storage-adapter schema and HTTP binding inventory."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from riverhog_storage_adapter_protocol import STORAGE_ADAPTER_PROTOCOL, StorageAdapterError

from riverhog_storage_adapter_support.http_binding import STORAGE_ADAPTER_HTTP_OPERATIONS

STORAGE_ADAPTER_SCHEMA_BUNDLE_FORMAT = "riverhog-storage-adapter-schema-bundle/v1"


def _model_type(value: object | None) -> type[BaseModel] | None:
    if value is None:
        return None
    if not isinstance(value, type) or not issubclass(value, BaseModel):
        raise TypeError("storage adapter HTTP declaration is not a Pydantic model")
    return value


def _model_name(value: object | None) -> str | None:
    model = _model_type(value)
    return model.__name__ if model is not None else None


def _schema_models() -> tuple[type[BaseModel], ...]:
    models: set[type[BaseModel]] = {StorageAdapterError}
    for operation in STORAGE_ADAPTER_HTTP_OPERATIONS:
        for model in (operation.request_type, operation.response_type):
            model_type = _model_type(model)
            if model_type is not None:
                models.add(model_type)
    return tuple(sorted(models, key=lambda model: model.__name__))


def _http_operation_inventory() -> list[dict[str, Any]]:
    return [
        {
            "method": operation.method,
            "path": operation.path,
            "request": {
                "kind": operation.request_kind,
                "schema": _model_name(operation.request_type),
            },
            "response": {
                "kind": operation.response_kind,
                "schema": _model_name(operation.response_type),
                "statuses": list(operation.success_statuses),
                "headers": [header.name for header in operation.response_headers],
            },
            "errors": [{"code": error.code, "status": error.status} for error in operation.errors],
        }
        for operation in STORAGE_ADAPTER_HTTP_OPERATIONS
    ]


def storage_adapter_schema_bundle() -> dict[str, Any]:
    return {
        "format": STORAGE_ADAPTER_SCHEMA_BUNDLE_FORMAT,
        "protocol": STORAGE_ADAPTER_PROTOCOL,
        "compatibility": {
            "unknown_fields": "reject",
            "provider_ontology": "private",
        },
        "http_binding": {"operations": _http_operation_inventory()},
        "schemas": {
            model.__name__: model.model_json_schema(mode="validation") for model in _schema_models()
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

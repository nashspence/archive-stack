"""Machine-readable target wire schemas for non-Python implementations."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from http_api_contracts import http_operation_inventory, structural_model_catalog
from stove0_target_protocol import (
    EFFECT_TARGET_PROTOCOL,
    TARGET_HTTP_OPERATIONS,
    TRANSFORM_TARGET_PROTOCOL,
    OperationContract,
    TargetContract,
    TargetJobRequest,
    TargetJobStatus,
    TargetPreflightRequest,
    TargetPreflightResponse,
)
from stove0_target_protocol.jcs import canonical_json_bytes, canonical_json_sha256

from stove0_target_support.conformance import TargetConformanceResult

TARGET_SCHEMA_BUNDLE_FORMAT = "stove0-target-schema-bundle/v1"

_ADDITIONAL_MODELS = (
    OperationContract,
    TargetContract,
    TargetPreflightRequest,
    TargetPreflightResponse,
    TargetJobRequest,
    TargetJobStatus,
)


def target_schema_bundle() -> dict[str, Any]:
    """Return structural models and the exact v1 target HTTP inventory."""

    payload: dict[str, Any] = {
        "format": TARGET_SCHEMA_BUNDLE_FORMAT,
        "protocols": [TRANSFORM_TARGET_PROTOCOL, EFFECT_TARGET_PROTOCOL],
        "compatibility": {
            "unknown_fields": "reject",
            "unknown_protocol_revision": "reject",
            "contract_identity": "rfc8785-sha256",
        },
        "authorities": {
            "structural_models": "schemas",
            "http_operations": "http_binding.operations",
            "semantic_acceptance": "semantic_acceptance",
        },
        "http_binding": {
            "operations": http_operation_inventory(TARGET_HTTP_OPERATIONS),
        },
        "semantic_acceptance": {
            "kind": "operation-contract",
            "binding": "OperationContract.intent_semantics",
            "identity": ["profile_id", "profile_sha256"],
            "request_response_relations": "required",
        },
        "schemas": structural_model_catalog(
            TARGET_HTTP_OPERATIONS,
            additional_models=(*_ADDITIONAL_MODELS, TargetConformanceResult),
        ),
    }
    return {**payload, "bundle_sha256": canonical_json_sha256(payload)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stove0-target-schemas",
        description="Emit the machine-readable stove0 target v1 wire schemas.",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compact", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    bundle = target_schema_bundle()
    content = (
        canonical_json_bytes(bundle)
        if args.compact
        else (json.dumps(bundle, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    if args.output is None:
        print(content.decode("utf-8"), end="" if content.endswith(b"\n") else "\n")
    else:
        args.output.write_bytes(content)
    return 0


__all__ = ["TARGET_SCHEMA_BUNDLE_FORMAT", "main", "target_schema_bundle"]

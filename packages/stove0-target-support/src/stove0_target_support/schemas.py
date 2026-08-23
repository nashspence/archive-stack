"""Machine-readable target wire schemas for non-Python implementations."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from stove0_target_protocol import (
    EFFECT_TARGET_PROTOCOL,
    TRANSFORM_TARGET_PROTOCOL,
    OperationContract,
    TargetCancelRequest,
    TargetContract,
    TargetJobRequest,
    TargetJobStatus,
    TargetPreflightRequest,
    TargetPreflightResponse,
)
from stove0_target_protocol.jcs import canonical_json_bytes, canonical_json_sha256

TARGET_SCHEMA_BUNDLE_FORMAT = "stove0-target-schema-bundle/v1"

_SCHEMA_MODELS: tuple[type[BaseModel], ...] = (
    OperationContract,
    TargetContract,
    TargetPreflightRequest,
    TargetPreflightResponse,
    TargetJobRequest,
    TargetJobStatus,
    TargetCancelRequest,
)


def target_schema_bundle() -> dict[str, Any]:
    """Return the complete stable v1 target HTTP wire-schema bundle."""

    payload: dict[str, Any] = {
        "format": TARGET_SCHEMA_BUNDLE_FORMAT,
        "protocols": [TRANSFORM_TARGET_PROTOCOL, EFFECT_TARGET_PROTOCOL],
        "compatibility": {
            "unknown_fields": "reject",
            "unknown_protocol_revision": "reject",
            "contract_identity": "rfc8785-sha256",
        },
        "http_binding": {
            "GET /v1/target": "TargetContract",
            "POST /v1/preflight": {
                "request": "TargetPreflightRequest",
                "response": "TargetPreflightResponse",
            },
            "PUT /v1/jobs/{job_id}": {
                "request": "TargetJobRequest",
                "response": "TargetJobStatus",
            },
            "GET /v1/jobs/{job_id}": "TargetJobStatus",
            "POST /v1/jobs/{job_id}/cancel": {
                "request": "TargetCancelRequest",
                "response": "TargetJobStatus",
            },
        },
        "schemas": {
            model.__name__: model.model_json_schema(mode="validation") for model in _SCHEMA_MODELS
        },
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

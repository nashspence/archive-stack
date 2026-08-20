"""Machine-readable observer wire schemas for non-Python implementations."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from stove0_observer_protocol import (
    OBSERVER_PROTOCOL,
    ObservationInvocation,
    ObservationRequest,
    ObservationResult,
    ObserverContract,
    ObserverDescriptor,
    canonical_json_bytes,
    canonical_json_sha256,
)

OBSERVER_SCHEMA_BUNDLE_FORMAT = "stove0-observer-schema-bundle/v1"

_SCHEMA_MODELS: tuple[type[BaseModel], ...] = (
    ObserverContract,
    ObserverDescriptor,
    ObservationRequest,
    ObservationInvocation,
    ObservationResult,
)


def observer_schema_bundle() -> dict[str, Any]:
    """Return the complete stable v1 observer HTTP wire-schema bundle."""

    payload: dict[str, Any] = {
        "format": OBSERVER_SCHEMA_BUNDLE_FORMAT,
        "protocol": OBSERVER_PROTOCOL,
        "compatibility": {
            "unknown_fields": "reject",
            "unknown_protocol_revision": "reject",
            "contract_identity": "canonical-json-sha256",
        },
        "http_binding": {
            "GET /v1/observer": "ObserverDescriptor",
            "POST /v1/observe": {
                "request": "ObservationInvocation",
                "response": "ObservationResult",
            },
        },
        "schemas": {
            model.__name__: model.model_json_schema(mode="validation") for model in _SCHEMA_MODELS
        },
    }
    return {**payload, "bundle_sha256": canonical_json_sha256(payload)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stove0-observer-schemas",
        description="Emit the machine-readable stove0 observer v1 wire schemas.",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compact", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    bundle = observer_schema_bundle()
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


__all__ = ["OBSERVER_SCHEMA_BUNDLE_FORMAT", "main", "observer_schema_bundle"]

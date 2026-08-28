"""Machine-readable sampler protocol schema inventory."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
from collections.abc import Sequence
from typing import Any, Final

from http_api_contracts import http_operation_inventory, structural_model_catalog
from stove0_review_sampler_protocol import (
    SAMPLER_HTTP_OPERATIONS,
    SamplerDescriptor,
    SamplerRequest,
    SamplerResult,
)

from stove0_review_sampler_support.conformance import SamplerConformanceResult

SAMPLER_SCHEMA_BUNDLE_FORMAT: Final = "stove0-review-sampler-schema-bundle/v1"


def sampler_schema_bundle() -> dict[str, Any]:
    return {
        "format": SAMPLER_SCHEMA_BUNDLE_FORMAT,
        "protocol": "stove0-review-sampler/v1",
        "authorities": {
            "structural_models": "schemas",
            "http_operations": "http_binding.operations",
            "semantic_acceptance": "semantic_acceptance",
        },
        "http_binding": {
            "operations": http_operation_inventory(SAMPLER_HTTP_OPERATIONS),
        },
        "semantic_acceptance": {
            "kind": "request-bound-result",
            "validator": "validate_result",
        },
        "schemas": structural_model_catalog(
            SAMPLER_HTTP_OPERATIONS,
            additional_models=(
                SamplerDescriptor,
                SamplerRequest,
                SamplerResult,
                SamplerConformanceResult,
            ),
        ),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stove0-review-sampler-schemas")
    parser.add_argument(
        "--version",
        action="version",
        version=importlib.metadata.version("stove0-review-sampler-support"),
    )
    parser.parse_args(argv)
    print(json.dumps(sampler_schema_bundle(), indent=2, sort_keys=True))
    return 0


__all__ = ["SAMPLER_SCHEMA_BUNDLE_FORMAT", "main", "sampler_schema_bundle"]

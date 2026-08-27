"""Machine-readable sampler protocol schema inventory."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
from collections.abc import Sequence
from typing import Any, Final

from stove0_review_sampler_protocol import (
    SamplerDescriptor,
    SamplerRequest,
    SamplerResult,
)

SAMPLER_SCHEMA_BUNDLE_FORMAT: Final = "stove0-review-sampler-schema-bundle/v1"


def sampler_schema_bundle() -> dict[str, Any]:
    return {
        "format": SAMPLER_SCHEMA_BUNDLE_FORMAT,
        "protocol": "stove0-review-sampler/v1",
        "endpoints": {
            "GET /v1/sampler": "SamplerDescriptor",
            "POST /v1/sample": {
                "request": "SamplerRequest",
                "response": "SamplerResult",
            },
        },
        "schemas": {
            "SamplerDescriptor": SamplerDescriptor.model_json_schema(),
            "SamplerRequest": SamplerRequest.model_json_schema(),
            "SamplerResult": SamplerResult.model_json_schema(),
        },
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

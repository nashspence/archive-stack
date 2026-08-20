"""Consumer-runnable conformance checks for terminal review samplers."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator
from stove0_sampler_protocol import (
    SamplerDescriptor,
    SamplerRequest,
    SamplerResult,
    validate_result,
)

from stove0_sampler_support.client import ReviewSamplerClient


class SamplerClient(Protocol):
    def descriptor(self, *, refresh: bool = False) -> SamplerDescriptor: ...

    def sample(self, request: SamplerRequest) -> SamplerResult: ...


def conformance_report(
    client: SamplerClient,
    *,
    request: SamplerRequest | None = None,
) -> dict[str, Any]:
    descriptor = client.descriptor()
    report: dict[str, Any] = {
        "status": "conformant",
        "protocol": descriptor.protocol,
        "implementation_id": descriptor.implementation_id,
        "implementation_version": descriptor.implementation_version,
        "source_revision": descriptor.source_revision,
        "image_digest": descriptor.image_digest,
        "descriptor_sha256": descriptor.descriptor_sha256,
        "primary_operation_id": descriptor.primary_operation_id,
        "primary_operation_contract_sha256": (descriptor.primary_operation_contract_sha256),
        "portable_intent_schema_sha256": descriptor.portable_intent_schema.sha256,
        "output_role": descriptor.output_role,
    }
    if request is None:
        return report
    if request.sampler_descriptor_sha256 != descriptor.descriptor_sha256:
        raise RuntimeError("sampler request does not bind the deployed descriptor")
    Draft202012Validator(descriptor.portable_intent_schema.document).validate(
        request.portable_intent
    )
    result = client.sample(request)
    validate_result(result, request, descriptor)
    report["sample"] = result.model_dump(mode="json", exclude_none=True)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stove0-sampler-conformance",
        description="Check a deployed terminal review sampler's v1 contract.",
    )
    parser.add_argument("base_url")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--allow-insecure-http", action="store_true")
    parser.add_argument(
        "--version",
        action="version",
        version=importlib.metadata.version("stove0-sampler-support"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    token = args.token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("sampler token file is empty")
    request = (
        None
        if args.request is None
        else SamplerRequest.model_validate_json(args.request.read_text(encoding="utf-8"))
    )
    with ReviewSamplerClient(
        args.base_url,
        token,
        allow_insecure_http=args.allow_insecure_http,
    ) as client:
        report = conformance_report(client, request=request)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


__all__ = ["SamplerClient", "conformance_report", "main"]

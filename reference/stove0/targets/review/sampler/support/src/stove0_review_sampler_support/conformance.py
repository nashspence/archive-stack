"""Consumer-runnable conformance checks for terminal review samplers."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, Protocol, Self

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, model_validator
from stove0_review_sampler_client.client import ReviewSamplerClient
from stove0_review_sampler_protocol import (
    SamplerDescriptor,
    SamplerRequest,
    SamplerResult,
    validate_result,
)

SAMPLER_CONFORMANCE_RESULT: Literal["stove0-review-sampler-conformance-result/v1"] = (
    "stove0-review-sampler-conformance-result/v1"
)


class _SamplerConformanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SamplerConformanceCoverage(_SamplerConformanceModel):
    advertised: Literal[1] = 1
    exercised: int = Field(ge=0, le=1, strict=True)
    complete: bool

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.complete != (self.exercised == 1):
            raise ValueError("sampler conformance coverage is inconsistent")
        return self


class SamplerConformanceResult(_SamplerConformanceModel):
    format: Literal["stove0-review-sampler-conformance-result/v1"] = SAMPLER_CONFORMANCE_RESULT
    status: Literal["conformant", "inspected"]
    sampler: SamplerDescriptor
    coverage: SamplerConformanceCoverage
    sampling: Literal["exercised", "not-exercised"]
    request: SamplerRequest | None = None
    sample: SamplerResult | None = None

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        exercised = self.request is not None and self.sample is not None
        if (
            (self.request is None) != (self.sample is None)
            or self.coverage.exercised != (1 if exercised else 0)
            or self.coverage.complete != exercised
            or self.status != ("conformant" if exercised else "inspected")
            or self.sampling != ("exercised" if exercised else "not-exercised")
        ):
            raise ValueError("sampler conformance result is inconsistent")
        if exercised:
            assert self.request is not None and self.sample is not None
            validate_result(self.sample, self.request, self.sampler)
        return self


class SamplerClient(Protocol):
    def descriptor(self, *, refresh: bool = False) -> SamplerDescriptor: ...

    def sample(self, request: SamplerRequest) -> SamplerResult: ...


def conformance_report(
    client: SamplerClient,
    *,
    request: SamplerRequest | None = None,
) -> SamplerConformanceResult:
    descriptor = client.descriptor()
    exercised = request is not None
    report: dict[str, object] = {
        "status": "conformant" if exercised else "inspected",
        "sampler": descriptor,
        "coverage": {
            "advertised": 1,
            "exercised": 1 if exercised else 0,
            "complete": exercised,
        },
        "sampling": "exercised" if exercised else "not-exercised",
    }
    if request is None:
        return SamplerConformanceResult.model_validate(report)
    if request.sampler_descriptor_sha256 != descriptor.descriptor_sha256:
        raise RuntimeError("sampler request does not bind the deployed descriptor")
    Draft202012Validator(descriptor.portable_intent_schema.document).validate(
        request.portable_intent
    )
    result = client.sample(request)
    validate_result(result, request, descriptor)
    report["request"] = request
    report["sample"] = result
    return SamplerConformanceResult.model_validate(report)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stove0-review-sampler-conformance",
        description="Check a deployed terminal review sampler's v1 contract.",
    )
    parser.add_argument("base_url")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--allow-insecure-http", action="store_true")
    parser.add_argument(
        "--version",
        action="version",
        version=importlib.metadata.version("stove0-review-sampler-support"),
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
    print(
        json.dumps(
            report.model_dump(mode="json", exclude_none=True),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


__all__ = [
    "SAMPLER_CONFORMANCE_RESULT",
    "SamplerClient",
    "SamplerConformanceResult",
    "conformance_report",
    "main",
]

"""Consumer-runnable conformance entrypoint for deployed Munchy targets."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from munchy_target_support.client import TransformTargetClient, protocol_report
from munchy_target_support.operations import operation_contract
from munchy_target_support.protocol import (
    TargetJobRequest,
    TargetPreflightRequest,
    validate_preflight_response_against_request,
    validate_status_against_request,
)


def conformance_report(
    client: TransformTargetClient,
    *,
    job_request: TargetJobRequest | None = None,
) -> dict[str, Any]:
    report = protocol_report(client)
    if job_request is None:
        return report
    preflight = client.preflight(
        declaration := TargetPreflightRequest(
            operation_id=job_request.plan.operation_id,
            operation_contract_sha256=job_request.plan.operation_contract_sha256,
            workspace_id=job_request.plan.workspace_id,
            inputs=job_request.plan.inputs,
            intent=job_request.plan.intent,
            target_options=job_request.plan.target_options,
        )
    )
    validate_preflight_response_against_request(preflight, declaration)
    if preflight.plan != job_request.plan:
        raise RuntimeError("target preflight did not reproduce the submitted plan")
    first = client.put_job(job_request)
    second = client.put_job(job_request)
    operation = operation_contract(job_request.plan.operation_id)
    validate_status_against_request(first, job_request, operation)
    validate_status_against_request(second, job_request, operation)
    if (
        first.job_id,
        first.attempt,
        first.request_sha256,
        first.plan_sha256,
    ) != (
        second.job_id,
        second.attempt,
        second.request_sha256,
        second.plan_sha256,
    ):
        raise RuntimeError("target submission did not preserve the accepted job identity")
    report.update(
        {
            "preflight": preflight.model_dump(mode="json"),
            "submission": second.model_dump(mode="json", exclude_none=True),
        }
    )
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="munchy-target-conformance",
        description="Check a deployed Munchy transform target's published v1 contract.",
    )
    parser.add_argument("base_url")
    parser.add_argument(
        "--job-request",
        type=Path,
        help="optional JSON TargetJobRequest used to check preflight and idempotent submission",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    request = None
    if args.job_request is not None:
        request = TargetJobRequest.model_validate_json(args.job_request.read_text(encoding="utf-8"))
    report = conformance_report(TransformTargetClient(args.base_url), job_request=request)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


__all__ = ["conformance_report", "main"]

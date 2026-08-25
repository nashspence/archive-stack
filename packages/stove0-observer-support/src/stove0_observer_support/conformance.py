"""Consumer-runnable conformance checks for content observers."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator
from stove0_observer_client.client import ContentObserverClient
from stove0_observer_protocol import (
    ObservationInvocation,
    ObserverDescriptor,
    canonical_json_bytes,
    validate_observation_result,
)


class ObserverClient(Protocol):
    def descriptor(self) -> ObserverDescriptor: ...

    def observe(
        self,
        invocation: ObservationInvocation,
        *,
        descriptor: ObserverDescriptor,
    ) -> Any: ...


def conformance_report(
    client: ObserverClient,
    *,
    invocation: ObservationInvocation | None = None,
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
        "contracts": [
            {
                "contract_id": support.contract_id,
                "contract_sha256": support.contract_sha256,
                "options_schema_sha256": support.options_schema.sha256,
                "facts_schema_sha256": support.facts_schema.sha256,
                "preferred_subject_batch_size": support.preferred_subject_batch_size,
                "maximum_result_bytes": support.maximum_result_bytes,
            }
            for support in descriptor.contracts
        ],
    }
    if invocation is None:
        return report
    request = invocation.request
    support = descriptor.support_for(request.observer_contract_id)
    if request.observer_contract_sha256 != support.contract_sha256:
        raise RuntimeError("invocation does not bind the observer's published contract")
    Draft202012Validator(support.options_schema.document).validate(request.options)
    result = client.observe(invocation, descriptor=descriptor)
    validate_observation_result(result, request, descriptor)
    if result.state == "observed":
        Draft202012Validator(support.facts_schema.document).validate(result.facts)
    if len(canonical_json_bytes(result.model_dump(mode="json", exclude_none=True))) > (
        request.maximum_result_bytes
    ):
        raise RuntimeError("observer result exceeds the invocation limit")
    report["observation"] = result.model_dump(mode="json", exclude_none=True)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stove0-observer-conformance",
        description="Check a deployed stove0 content observer's v1 contract.",
    )
    parser.add_argument("base_url")
    parser.add_argument(
        "--invocation",
        type=Path,
        help="optional JSON ObservationInvocation used for a complete black-box check",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    invocation = None
    if args.invocation is not None:
        invocation = ObservationInvocation.model_validate_json(
            args.invocation.read_text(encoding="utf-8")
        )
    report = conformance_report(
        ContentObserverClient(args.base_url),
        invocation=invocation,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


__all__ = ["ObserverClient", "conformance_report", "main"]

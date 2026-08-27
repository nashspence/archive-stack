"""Consumer-runnable conformance checks for stove0 targets."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator
from stove0_target_client.client import (
    TargetClient as HttpTargetClient,
)
from stove0_target_client.client import (
    TargetProtocolError,
)
from stove0_target_protocol import (
    JSON_SCHEMA_ONLY_SEMANTIC_PROFILE,
    OperationContract,
    SemanticIntentConformanceVectors,
    TargetContract,
    TargetJobRequest,
    TargetJobStatus,
    TargetPreflightRequest,
    TargetPreflightResponse,
    validate_declaration_against_operation,
    validate_preflight_response_against_request,
    validate_status_against_request,
)


class TargetClient(Protocol):
    def contract(self) -> TargetContract: ...

    def preflight(self, request: TargetPreflightRequest) -> TargetPreflightResponse: ...

    def put_job(
        self,
        request: TargetJobRequest,
        *,
        operation: OperationContract,
    ) -> TargetJobStatus: ...

    def status(
        self,
        request: TargetJobRequest,
        *,
        operation: OperationContract,
    ) -> TargetJobStatus: ...


def conformance_report(
    client: TargetClient,
    *,
    operation: OperationContract | None = None,
    job_request: TargetJobRequest | None = None,
    semantic_vectors: SemanticIntentConformanceVectors | None = None,
) -> dict[str, Any]:
    contract = client.contract()
    operation_reports: list[dict[str, Any]] = []
    for item in contract.operations:
        entry: dict[str, Any] = {
            "operation_id": item.operation_id,
            "operation_contract_sha256": item.operation_contract_sha256,
            "result_kind": item.result_kind,
            "options_schema_sha256": item.options_schema.sha256,
            "semantic_conformance": "not-exercised",
        }
        if operation is not None and operation.id == item.operation_id:
            if operation.contract_sha256 != item.operation_contract_sha256:
                raise RuntimeError("operation contract differs from the deployed target")
            entry.update(
                {
                    "intent_semantics_id": operation.intent_semantics.id,
                    "intent_semantics_sha256": operation.intent_semantics.profile_sha256,
                    "intent_semantics_conformance_vectors_sha256": (
                        operation.intent_semantics.conformance_vectors_sha256
                    ),
                }
            )
        operation_reports.append(entry)
    report: dict[str, Any] = {
        "status": "contract-inspected",
        "protocol": contract.protocol,
        "implementation_id": contract.implementation_id,
        "implementation_version": contract.implementation_version,
        "source_revision": contract.source_revision,
        "image_digest": contract.image_digest,
        "target_contract_sha256": contract.contract_sha256,
        "transport": contract.transport,
        "operations": operation_reports,
    }
    if job_request is None:
        if semantic_vectors is not None:
            raise ValueError("semantic-vector conformance requires a job request")
        return report
    if operation is None:
        raise ValueError("job conformance requires the matching operation contract")
    declaration = job_request.declaration
    validate_declaration_against_operation(declaration.plan, operation)
    support = contract.support_for(declaration.plan.operation_id)
    if (
        support.operation_contract_sha256 != operation.contract_sha256
        or declaration.plan.target_contract_sha256 != contract.contract_sha256
    ):
        raise RuntimeError("job request does not bind the deployed target contract")
    Draft202012Validator(operation.intent_schema.document).validate(declaration.plan.intent)
    Draft202012Validator(support.options_schema.document).validate(declaration.plan.target_options)
    observations = declaration.controller_evidence.execution_envelope.workflow_plan.observations

    semantic_proof: dict[str, Any]
    if operation.intent_semantics == JSON_SCHEMA_ONLY_SEMANTIC_PROFILE:
        if semantic_vectors is not None:
            raise ValueError("schema-only operation does not accept semantic conformance vectors")
        semantic_proof = {
            "profile_id": operation.intent_semantics.id,
            "profile_sha256": operation.intent_semantics.profile_sha256,
            "status": "schema-only",
        }
    else:
        expected_vectors_sha256 = operation.intent_semantics.conformance_vectors_sha256
        if semantic_vectors is None:
            raise ValueError(
                "non-schema-only operation requires its exact semantic conformance vectors"
            )
        if (
            semantic_vectors.profile_id != operation.intent_semantics.id
            or semantic_vectors.sha256 != expected_vectors_sha256
        ):
            raise ValueError("semantic conformance vectors differ from the operation profile")
        accepted_ids: list[str] = []
        rejected_ids: list[str] = []
        for vector in semantic_vectors.vectors:
            Draft202012Validator(operation.intent_schema.document).validate(vector.intent)
            vector_request = TargetPreflightRequest(
                protocol=declaration.plan.protocol,
                operation_id=declaration.plan.operation_id,
                operation_contract_sha256=declaration.plan.operation_contract_sha256,
                inputs=declaration.plan.inputs,
                observations=observations,
                intent=vector.intent,
                target_options=declaration.plan.target_options,
            )
            try:
                vector_preflight = client.preflight(vector_request)
                validate_preflight_response_against_request(vector_preflight, vector_request)
            except TargetProtocolError as exc:
                if vector.accepted:
                    raise RuntimeError(
                        f"target rejected accepted semantic vector: {vector.id}"
                    ) from exc
                if exc.observed_status != 400 or exc.code != "invalid_target_request":
                    raise RuntimeError(
                        f"target did not semantically reject vector: {vector.id}"
                    ) from exc
                rejected_ids.append(vector.id)
            else:
                if not vector.accepted:
                    raise RuntimeError(f"target accepted rejected semantic vector: {vector.id}")
                accepted_ids.append(vector.id)
        semantic_proof = {
            "profile_id": operation.intent_semantics.id,
            "profile_sha256": operation.intent_semantics.profile_sha256,
            "conformance_vectors_sha256": semantic_vectors.sha256,
            "accepted_vector_ids": accepted_ids,
            "rejected_vector_ids": rejected_ids,
            "status": "exercised",
        }
    preflight_request = TargetPreflightRequest(
        protocol=declaration.plan.protocol,
        operation_id=declaration.plan.operation_id,
        operation_contract_sha256=declaration.plan.operation_contract_sha256,
        inputs=declaration.plan.inputs,
        observations=observations,
        intent=declaration.plan.intent,
        target_options=declaration.plan.target_options,
    )
    preflight = client.preflight(preflight_request)
    validate_preflight_response_against_request(preflight, preflight_request)
    if preflight.plan != declaration.plan:
        raise RuntimeError("target preflight did not reproduce the submitted plan")
    first = client.put_job(job_request, operation=operation)
    second = client.put_job(job_request, operation=operation)
    observed = client.status(job_request, operation=operation)
    validate_status_against_request(first, job_request, operation)
    validate_status_against_request(second, job_request, operation)
    validate_status_against_request(observed, job_request, operation)
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
    for operation_report in operation_reports:
        if operation_report["operation_id"] == operation.id:
            operation_report["semantic_conformance"] = semantic_proof["status"]
    report.update(
        {
            "status": "conformant",
            "semantic_conformance": semantic_proof,
            "preflight": preflight.model_dump(mode="json", exclude_none=True),
            "submission": second.model_dump(mode="json", exclude_none=True),
            "job_status": observed.model_dump(mode="json", exclude_none=True),
        }
    )
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stove0-target-conformance",
        description="Check a deployed stove0 transform target's v1 contract.",
    )
    parser.add_argument("base_url")
    parser.add_argument("--operation-contract", type=Path)
    parser.add_argument("--job-request", type=Path)
    parser.add_argument("--semantic-vectors", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    operation = None
    request = None
    semantic_vectors = None
    if args.operation_contract is not None:
        operation = OperationContract.model_validate_json(
            args.operation_contract.read_text(encoding="utf-8")
        )
    if args.job_request is not None:
        request = TargetJobRequest.model_validate_json(args.job_request.read_text(encoding="utf-8"))
    if args.semantic_vectors is not None:
        semantic_vectors = SemanticIntentConformanceVectors.model_validate_json(
            args.semantic_vectors.read_text(encoding="utf-8")
        )
    report = conformance_report(
        HttpTargetClient(args.base_url),
        operation=operation,
        job_request=request,
        semantic_vectors=semantic_vectors,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


__all__ = ["TargetClient", "conformance_report", "main"]

"""Consumer-runnable conformance checks for stove0 targets."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, Self

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, model_validator
from stove0_target_client.client import (
    TargetClient as HttpTargetClient,
)
from stove0_target_client.client import (
    TargetProtocolError,
)
from stove0_target_protocol import (
    JSON_SCHEMA_ONLY_SEMANTIC_PROFILE,
    AcceptedTargetJob,
    OperationContract,
    SemanticIntentConformanceVectors,
    TargetContract,
    TargetJobRequest,
    TargetJobStatus,
    TargetPreflightRequest,
    TargetPreflightResponse,
    TargetResultKind,
    validate_declaration_against_operation,
    validate_preflight_response_against_request,
    validate_status_against_request,
)

TARGET_CONFORMANCE_RESULT: Literal["stove0-target-conformance-result/v1"] = (
    "stove0-target-conformance-result/v1"
)


class _TargetConformanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TargetConformanceCoverage(_TargetConformanceModel):
    advertised: int = Field(ge=0, strict=True)
    exercised: int = Field(ge=0, strict=True)
    complete: bool

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.exercised > self.advertised or self.complete != (self.exercised == self.advertised):
            raise ValueError("target conformance coverage is inconsistent")
        return self


class TargetOperationConformance(_TargetConformanceModel):
    operation_id: str
    operation_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_kind: TargetResultKind
    options_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_conformance: Literal["not-exercised", "schema-only", "exercised"]
    intent_semantics_id: str | None = None
    intent_semantics_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    intent_semantics_conformance_vectors_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )


class TargetSemanticConformance(_TargetConformanceModel):
    profile_id: str
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["schema-only", "exercised"]
    conformance_vectors_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    vectors: SemanticIntentConformanceVectors | None = None
    accepted_vector_ids: tuple[str, ...] = ()
    rejected_vector_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_vectors(self) -> Self:
        if self.status == "schema-only":
            if (
                self.conformance_vectors_sha256 is not None
                or self.vectors is not None
                or self.accepted_vector_ids
                or self.rejected_vector_ids
            ):
                raise ValueError("schema-only target semantics cannot include vector evidence")
            return self
        if self.vectors is None or self.conformance_vectors_sha256 != self.vectors.sha256:
            raise ValueError("target semantic conformance lacks its exact vectors")
        if self.profile_id != self.vectors.profile_id:
            raise ValueError("target semantic vectors name a different profile")
        accepted = tuple(item.id for item in self.vectors.vectors if item.accepted)
        rejected = tuple(item.id for item in self.vectors.vectors if not item.accepted)
        if self.accepted_vector_ids != accepted or self.rejected_vector_ids != rejected:
            raise ValueError("target semantic-vector evidence differs from its vectors")
        return self


class TargetOperationConformanceEvidence(_TargetConformanceModel):
    operation_id: str
    operation: OperationContract
    semantic_conformance: TargetSemanticConformance
    preflight_request: TargetPreflightRequest
    preflight: TargetPreflightResponse
    accepted_job: AcceptedTargetJob
    submission: TargetJobStatus
    job_status: TargetJobStatus


class TargetConformanceResult(_TargetConformanceModel):
    format: Literal["stove0-target-conformance-result/v1"] = TARGET_CONFORMANCE_RESULT
    status: Literal["conformant", "partially-exercised", "inspected"]
    target: TargetContract
    coverage: TargetConformanceCoverage
    operations: tuple[TargetOperationConformance, ...]
    operation_evidence: tuple[TargetOperationConformanceEvidence, ...] = ()

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        expected_status = (
            "conformant"
            if self.coverage.complete
            else "partially-exercised"
            if self.coverage.exercised
            else "inspected"
        )
        if self.status != expected_status:
            raise ValueError("target conformance status differs from its coverage")
        if self.coverage.advertised != len(self.operations):
            raise ValueError("target conformance coverage differs from its operations")
        if self.coverage.exercised != len(self.operation_evidence):
            raise ValueError("target conformance evidence differs from its coverage")
        if len(self.operations) != len(self.target.operations):
            raise ValueError("target conformance operations differ from its contract")
        evidence_by_id = {item.operation_id: item for item in self.operation_evidence}
        if len(evidence_by_id) != len(self.operation_evidence):
            raise ValueError("target conformance repeats operation evidence")
        exercised = 0
        for report, support in zip(self.operations, self.target.operations, strict=True):
            if (
                report.operation_id != support.operation_id
                or report.operation_contract_sha256 != support.operation_contract_sha256
                or report.result_kind != support.result_kind
                or report.options_schema_sha256 != support.options_schema.sha256
            ):
                raise ValueError("target conformance operation differs from its contract")
            evidence = evidence_by_id.get(report.operation_id)
            if evidence is None:
                if report.semantic_conformance != "not-exercised":
                    raise ValueError("unexercised target operation claims semantic evidence")
                continue
            exercised += 1
            operation = evidence.operation
            if (
                evidence.operation_id != operation.id
                or operation.id != support.operation_id
                or operation.contract_sha256 != support.operation_contract_sha256
                or operation.result_kind != support.result_kind
                or report.intent_semantics_id != operation.intent_semantics.id
                or report.intent_semantics_sha256 != operation.intent_semantics.profile_sha256
                or report.intent_semantics_conformance_vectors_sha256
                != operation.intent_semantics.conformance_vectors_sha256
            ):
                raise ValueError("target conformance evidence differs from its operation")
            semantic = evidence.semantic_conformance
            if (
                semantic.profile_id != operation.intent_semantics.id
                or semantic.profile_sha256 != operation.intent_semantics.profile_sha256
                or report.semantic_conformance != semantic.status
            ):
                raise ValueError("target semantic evidence differs from its operation")
            if operation.intent_semantics == JSON_SCHEMA_ONLY_SEMANTIC_PROFILE:
                if semantic.status != "schema-only":
                    raise ValueError("schema-only target semantics claim vector evidence")
            elif (
                semantic.status != "exercised"
                or semantic.vectors is None
                or semantic.vectors.sha256 != operation.intent_semantics.conformance_vectors_sha256
            ):
                raise ValueError("target semantic vectors differ from its operation")

            accepted = evidence.accepted_job
            validate_declaration_against_operation(accepted.declaration.plan, operation)
            validate_preflight_response_against_request(
                evidence.preflight,
                evidence.preflight_request,
            )
            if (
                evidence.preflight.target != self.target
                or evidence.preflight.plan != accepted.declaration.plan
            ):
                raise ValueError("target preflight evidence differs from the accepted job")
            for status in (evidence.submission, evidence.job_status):
                validate_status_against_request(status, accepted, operation)
            if (
                evidence.submission.job_id,
                evidence.submission.request_sha256,
                evidence.submission.plan_sha256,
            ) != (
                evidence.job_status.job_id,
                evidence.job_status.request_sha256,
                evidence.job_status.plan_sha256,
            ):
                raise ValueError("target status evidence differs from its submission")
        if exercised != self.coverage.exercised:
            raise ValueError("target conformance coverage differs from its exact evidence")
        return self


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


def _single_operation_report(
    client: TargetClient,
    *,
    contract: TargetContract,
    operation: OperationContract | None = None,
    job_request: TargetJobRequest | None = None,
    semantic_vectors: SemanticIntentConformanceVectors | None = None,
) -> dict[str, Any]:
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
            "vectors": semantic_vectors,
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
            "operation": operation,
            "preflight_request": preflight_request,
            "preflight": preflight.model_dump(mode="json", exclude_none=True),
            "accepted_job": job_request.accepted(),
            "submission": second.model_dump(mode="json", exclude_none=True),
            "job_status": observed.model_dump(mode="json", exclude_none=True),
        }
    )
    return report


@dataclass(frozen=True, slots=True)
class TargetConformanceCase:
    """One advertised operation's complete black-box conformance input."""

    operation: OperationContract
    job_request: TargetJobRequest
    semantic_vectors: SemanticIntentConformanceVectors | None = None


def conformance_report(
    client: TargetClient,
    *,
    cases: Sequence[TargetConformanceCase] = (),
) -> TargetConformanceResult:
    """Report exact coverage, claiming conformance only for all advertised operations."""

    contract = client.contract()
    case_by_operation = {case.operation.id: case for case in cases}
    if len(case_by_operation) != len(cases):
        raise ValueError("target conformance cases must name unique operations")
    advertised_ids = {item.operation_id for item in contract.operations}
    if set(case_by_operation) - advertised_ids:
        raise ValueError("target conformance case names an unadvertised operation")

    report = _single_operation_report(client, contract=contract)
    evidence: list[dict[str, Any]] = []
    operation_reports = {str(item["operation_id"]): item for item in report["operations"]}
    for operation_id in sorted(case_by_operation):
        case = case_by_operation[operation_id]
        exercised = _single_operation_report(
            client,
            contract=contract,
            operation=case.operation,
            job_request=case.job_request,
            semantic_vectors=case.semantic_vectors,
        )
        operation_entry = next(
            item for item in exercised["operations"] if item["operation_id"] == operation_id
        )
        operation_reports[operation_id].update(operation_entry)
        evidence.append(
            {
                "operation_id": operation_id,
                "operation": exercised["operation"],
                "semantic_conformance": exercised["semantic_conformance"],
                "preflight_request": exercised["preflight_request"],
                "preflight": exercised["preflight"],
                "accepted_job": exercised["accepted_job"],
                "submission": exercised["submission"],
                "job_status": exercised["job_status"],
            }
        )

    exercised_count = len(evidence)
    complete = exercised_count == len(contract.operations)
    report.update(
        {
            "status": (
                "conformant"
                if complete
                else "partially-exercised"
                if exercised_count
                else "inspected"
            ),
            "coverage": {
                "advertised": len(contract.operations),
                "exercised": exercised_count,
                "complete": complete,
            },
            "operations": [operation_reports[item.operation_id] for item in contract.operations],
        }
    )
    if evidence:
        report["operation_evidence"] = evidence
    return TargetConformanceResult.model_validate(
        {
            "status": report["status"],
            "target": contract,
            "coverage": report["coverage"],
            "operations": report["operations"],
            "operation_evidence": report.get("operation_evidence", []),
        }
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stove0-target-conformance",
        description="Check a deployed stove0 transform target's v1 contract.",
    )
    parser.add_argument("base_url")
    parser.add_argument(
        "--case",
        type=Path,
        action="append",
        default=[],
        help=(
            "JSON object containing operation, job_request, and optional semantic_vectors "
            "(repeat once per advertised operation)"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    cases: list[TargetConformanceCase] = []
    for path in args.case:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or set(payload)
            - {
                "operation",
                "job_request",
                "semantic_vectors",
            }
            or not {"operation", "job_request"} <= set(payload)
        ):
            raise ValueError("target conformance case fields are invalid")
        vectors_payload = payload.get("semantic_vectors")
        cases.append(
            TargetConformanceCase(
                operation=OperationContract.model_validate(payload["operation"]),
                job_request=TargetJobRequest.model_validate(payload["job_request"]),
                semantic_vectors=(
                    None
                    if vectors_payload is None
                    else SemanticIntentConformanceVectors.model_validate(vectors_payload)
                ),
            )
        )
    report = conformance_report(
        HttpTargetClient(args.base_url),
        cases=cases,
    )
    print(
        json.dumps(
            report.model_dump(mode="json", exclude_none=True),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


__all__ = [
    "TARGET_CONFORMANCE_RESULT",
    "TargetClient",
    "TargetConformanceCase",
    "TargetConformanceResult",
    "conformance_report",
    "main",
]

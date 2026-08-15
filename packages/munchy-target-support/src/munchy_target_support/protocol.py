"""Published, hardware-neutral Munchy file-transform target protocol."""

from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, Self, cast

import rfc8785
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

TARGET_PROTOCOL: Literal["munchy-transform-target/v1"] = "munchy-transform-target/v1"
SHARED_DIRECTORY_TRANSPORT: Literal["shared-directory/v1"] = "shared-directory/v1"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
SEMANTIC_ID_PATTERN = r"^[a-z0-9](?:[a-z0-9._-]{0,118}[a-z0-9])?(?:/v1)?$"
REGISTRATION_ID_PATTERN = r"^[a-z0-9](?:[a-z0-9.-]{0,118}[a-z0-9])?$"
ARTIFACT_ID_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,158}[A-Za-z0-9])?$"
WORKSPACE_ID_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,158}[A-Za-z0-9])?$"

SemanticId = Annotated[str, StringConstraints(pattern=SEMANTIC_ID_PATTERN)]
Sha256 = Annotated[str, StringConstraints(pattern=SHA256_PATTERN)]

TargetJobState = Literal[
    "queued",
    "running",
    "canceling",
    "interrupted",
    "succeeded",
    "failed",
    "canceled",
]


def canonical_json_bytes(value: object) -> bytes:
    """Return RFC 8785 JSON Canonicalization Scheme bytes."""

    try:
        return rfc8785.dumps(cast(Any, value))
    except (rfc8785.CanonicalizationError, rfc8785.FloatDomainError) as exc:
        raise ValueError(f"value is not RFC 8785 canonicalizable: {exc}") from exc


def canonical_json_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _without_digest(model: BaseModel, field: str) -> dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True, exclude={field}, exclude_none=True)


def normalize_relative_posix_path(value: str) -> str:
    """Validate and normalize a workspace-relative POSIX artifact path."""

    if value != value.strip() or not value or "\\" in value or value.startswith("/"):
        raise ValueError("path must be a normalized relative POSIX path")
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("path must not contain empty, dot, or parent segments")
    normalized = path.as_posix()
    if normalized != value:
        raise ValueError("path must be a normalized relative POSIX path")
    return normalized


class TargetProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class JsonSchemaDocument(TargetProtocolModel):
    id: SemanticId
    sha256: Sha256
    document: dict[str, JsonValue] = Field(alias="schema")

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        if canonical_json_sha256(self.document) != self.sha256:
            raise ValueError("schema sha256 does not match its RFC 8785 canonical JSON")
        return self

    @classmethod
    def from_schema(cls, schema_id: str, schema: dict[str, JsonValue]) -> JsonSchemaDocument:
        return cls(id=schema_id, sha256=canonical_json_sha256(schema), schema=schema)


class InputArtifactContract(TargetProtocolModel):
    role: SemanticId
    minimum: int = Field(default=1, ge=0)
    maximum: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_cardinality(self) -> Self:
        if self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("input artifact minimum must be <= maximum")
        return self


class OutputArtifactContract(TargetProtocolModel):
    role: SemanticId
    minimum: int = Field(default=1, ge=0)
    maximum: int | None = Field(default=None, ge=1)
    derived_from_roles: tuple[SemanticId, ...] = Field(min_length=1)

    @field_validator("derived_from_roles")
    @classmethod
    def unique_derivation_roles(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("derived_from_roles must be unique")
        return value

    @model_validator(mode="after")
    def validate_cardinality(self) -> Self:
        if self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("output artifact minimum must be <= maximum")
        return self


class OperationContractPayload(TargetProtocolModel):
    id: SemanticId
    intent_schema: JsonSchemaDocument
    inputs: tuple[InputArtifactContract, ...] = Field(min_length=1)
    outputs: tuple[OutputArtifactContract, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_roles(self) -> Self:
        input_roles = [item.role for item in self.inputs]
        output_roles = [item.role for item in self.outputs]
        if len(input_roles) != len(set(input_roles)):
            raise ValueError("operation input roles must be unique")
        if len(output_roles) != len(set(output_roles)):
            raise ValueError("operation output roles must be unique")
        unknown = sorted(
            {
                role
                for output in self.outputs
                for role in output.derived_from_roles
                if role not in set(input_roles)
            }
        )
        if unknown:
            raise ValueError(
                "output derivation references unknown input role(s): " + ", ".join(unknown)
            )
        return self


class OperationContract(OperationContractPayload):
    contract_sha256: Sha256

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        if canonical_json_sha256(_without_digest(self, "contract_sha256")) != self.contract_sha256:
            raise ValueError("operation contract sha256 does not match its canonical payload")
        return self

    @classmethod
    def seal(cls, payload: OperationContractPayload) -> OperationContract:
        document = payload.model_dump(mode="json", by_alias=True, exclude_none=True)
        return cls(**document, contract_sha256=canonical_json_sha256(document))


class TargetOperationSupport(TargetProtocolModel):
    operation_id: SemanticId
    operation_contract_sha256: Sha256
    options_schema: JsonSchemaDocument


class TargetContractPayload(TargetProtocolModel):
    protocol: Literal["munchy-transform-target/v1"] = TARGET_PROTOCOL
    implementation_id: SemanticId
    implementation_version: str = Field(min_length=1, max_length=120)
    source_revision: str = Field(min_length=1, max_length=200)
    transport: Literal["shared-directory/v1"] = SHARED_DIRECTORY_TRANSPORT
    operations: tuple[TargetOperationSupport, ...] = Field(min_length=1)

    @field_validator("operations")
    @classmethod
    def unique_operations(
        cls, value: tuple[TargetOperationSupport, ...]
    ) -> tuple[TargetOperationSupport, ...]:
        ids = [item.operation_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("target operation IDs must be unique")
        return value


class TargetContract(TargetContractPayload):
    contract_sha256: Sha256

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        if canonical_json_sha256(_without_digest(self, "contract_sha256")) != self.contract_sha256:
            raise ValueError("target contract sha256 does not match its canonical payload")
        return self

    @classmethod
    def seal(cls, payload: TargetContractPayload) -> TargetContract:
        document = payload.model_dump(mode="json", by_alias=True, exclude_none=True)
        return cls(**document, contract_sha256=canonical_json_sha256(document))

    def support_for(self, operation_id: str) -> TargetOperationSupport:
        for support in self.operations:
            if support.operation_id == operation_id:
                return support
        raise ValueError(f"target does not support operation: {operation_id}")


class Artifact(TargetProtocolModel):
    id: str = Field(pattern=ARTIFACT_ID_PATTERN)
    role: SemanticId
    path: str = Field(min_length=1, max_length=4096)
    bytes: int = Field(ge=0)
    sha256: Sha256
    media_type: str | None = Field(default=None, min_length=1, max_length=255)
    derived_from: tuple[str, ...] = ()

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return normalize_relative_posix_path(value)

    @field_validator("derived_from")
    @classmethod
    def unique_sources(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("derived_from artifact IDs must be unique")
        for item in value:
            if re.fullmatch(ARTIFACT_ID_PATTERN, item) is None:
                raise ValueError(f"invalid derived_from artifact ID: {item}")
        return value


class TransformDeclaration(TargetProtocolModel):
    operation_id: SemanticId
    operation_contract_sha256: Sha256
    workspace_id: str = Field(pattern=WORKSPACE_ID_PATTERN)
    inputs: tuple[Artifact, ...] = Field(min_length=1)
    intent: dict[str, JsonValue]
    target_options: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("inputs")
    @classmethod
    def canonical_unique_inputs(cls, value: tuple[Artifact, ...]) -> tuple[Artifact, ...]:
        ids = [item.id for item in value]
        paths = [item.path for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("input artifact IDs must be unique")
        if len(paths) != len(set(paths)):
            raise ValueError("input artifact paths must be unique")
        if any(item.derived_from for item in value):
            raise ValueError("input artifacts must not declare derived_from")
        if ids != sorted(ids):
            raise ValueError("input artifacts must be ordered by ID")
        return value


class TargetPreflightRequest(TransformDeclaration):
    protocol: Literal["munchy-transform-target/v1"] = TARGET_PROTOCOL


class TransformPlanPayload(TransformDeclaration):
    protocol: Literal["munchy-transform-target/v1"] = TARGET_PROTOCOL
    target_implementation_id: SemanticId
    target_contract_sha256: Sha256
    effective_intent: dict[str, JsonValue]
    effective_target_options: dict[str, JsonValue]


class TransformPlan(TransformPlanPayload):
    plan_sha256: Sha256

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        if canonical_json_sha256(_without_digest(self, "plan_sha256")) != self.plan_sha256:
            raise ValueError("transform plan sha256 does not match its canonical payload")
        return self

    @classmethod
    def seal(cls, payload: TransformPlanPayload) -> TransformPlan:
        document = payload.model_dump(mode="json", by_alias=True, exclude_none=True)
        return cls(**document, plan_sha256=canonical_json_sha256(document))


class TargetPreflightResponse(TargetProtocolModel):
    accepted: Literal[True] = True
    target: TargetContract
    plan: TransformPlan

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        if self.plan.target_implementation_id != self.target.implementation_id:
            raise ValueError("plan target implementation does not match target contract")
        if self.plan.target_contract_sha256 != self.target.contract_sha256:
            raise ValueError("plan target digest does not match target contract")
        support = self.target.support_for(self.plan.operation_id)
        if support.operation_contract_sha256 != self.plan.operation_contract_sha256:
            raise ValueError("plan operation digest does not match target support")
        return self


class TargetJobRequestPayload(TargetProtocolModel):
    protocol: Literal["munchy-transform-target/v1"] = TARGET_PROTOCOL
    job_id: str = Field(pattern=WORKSPACE_ID_PATTERN)
    attempt: int = Field(default=1, ge=1)
    plan: TransformPlan

    @model_validator(mode="after")
    def workspace_matches_job(self) -> Self:
        if self.plan.workspace_id != self.job_id:
            raise ValueError("plan workspace_id must equal job_id")
        return self


class TargetJobRequest(TargetJobRequestPayload):
    request_sha256: Sha256

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        if canonical_json_sha256(_without_digest(self, "request_sha256")) != self.request_sha256:
            raise ValueError("job request sha256 does not match its canonical payload")
        return self

    @classmethod
    def seal(cls, payload: TargetJobRequestPayload) -> TargetJobRequest:
        document = payload.model_dump(mode="json", by_alias=True, exclude_none=True)
        return cls(**document, request_sha256=canonical_json_sha256(document))


class TargetCancelRequest(TargetProtocolModel):
    reason: str = Field(default="coordinator_canceled", min_length=1, max_length=500)


class TargetProgress(TargetProtocolModel):
    phase: str = Field(min_length=1, max_length=160)
    completed: int = Field(default=0, ge=0)
    total: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if self.total is not None and self.completed > self.total:
            raise ValueError("target progress completed must be <= total")
        return self


class TargetFailure(TargetProtocolModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,158}[a-z0-9]$")
    message: str = Field(min_length=1, max_length=2000)
    retryable: bool


class ExecutionToolEvidence(TargetProtocolModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,119}$")
    version: str = Field(min_length=1, max_length=1000)


class TargetExecutionEvidence(TargetProtocolModel):
    target: TargetContract
    operation: OperationContract
    effective_intent: dict[str, JsonValue]
    effective_target_options: dict[str, JsonValue]
    tools: tuple[ExecutionToolEvidence, ...] = ()

    @field_validator("tools")
    @classmethod
    def unique_tools(
        cls, value: tuple[ExecutionToolEvidence, ...]
    ) -> tuple[ExecutionToolEvidence, ...]:
        names = [item.name for item in value]
        if len(names) != len(set(names)):
            raise ValueError("execution tool evidence names must be unique")
        return value


class TargetJobStatus(TargetProtocolModel):
    protocol: Literal["munchy-transform-target/v1"] = TARGET_PROTOCOL
    job_id: str = Field(pattern=WORKSPACE_ID_PATTERN)
    attempt: int = Field(ge=1)
    request_sha256: Sha256
    plan_sha256: Sha256
    state: TargetJobState
    progress: TargetProgress
    outputs: tuple[Artifact, ...] = ()
    execution_evidence: TargetExecutionEvidence | None = None
    failure: TargetFailure | None = None
    started_at: str | None = None
    finished_at: str | None = None
    updated_at: str | None = None

    @model_validator(mode="after")
    def validate_terminal_shape(self) -> Self:
        terminal = self.state in {"succeeded", "failed", "canceled"}
        if self.state == "succeeded" and not self.outputs:
            raise ValueError("succeeded target jobs must publish output artifacts")
        if self.state != "succeeded" and self.outputs:
            raise ValueError("outputs may appear only on a succeeded job")
        if terminal and self.execution_evidence is None:
            raise ValueError("terminal target jobs require execution evidence")
        if not terminal and self.execution_evidence is not None:
            raise ValueError("execution evidence may appear only on terminal jobs")
        if self.state in {"failed", "canceled"} and self.failure is None:
            raise ValueError("failed and canceled target jobs require structured failure")
        if self.state not in {"failed", "canceled"} and self.failure is not None:
            raise ValueError("structured failure is valid only for failed or canceled jobs")
        if self.state in {"queued", "running", "canceling", "interrupted"} and self.finished_at:
            raise ValueError("nonterminal and interrupted jobs must not have finished_at")
        if terminal and not self.finished_at:
            raise ValueError("terminal target jobs require finished_at")
        return self


def validate_artifacts_against_operation(
    operation: OperationContract,
    *,
    inputs: tuple[Artifact, ...],
    outputs: tuple[Artifact, ...] | None = None,
) -> None:
    input_by_id = {item.id: item for item in inputs}
    input_counts = {contract.role: 0 for contract in operation.inputs}
    for artifact in inputs:
        if artifact.role not in input_counts:
            raise ValueError(f"operation does not accept input role: {artifact.role}")
        input_counts[artifact.role] += 1
    for input_contract in operation.inputs:
        count = input_counts[input_contract.role]
        if count < input_contract.minimum or (
            input_contract.maximum is not None and count > input_contract.maximum
        ):
            raise ValueError(f"input role cardinality is invalid: {input_contract.role}")
    if outputs is None:
        return
    output_counts = {contract.role: 0 for contract in operation.outputs}
    output_contracts = {contract.role: contract for contract in operation.outputs}
    for artifact in outputs:
        output_contract = output_contracts.get(artifact.role)
        if output_contract is None:
            raise ValueError(f"operation does not publish output role: {artifact.role}")
        if not artifact.derived_from:
            raise ValueError(f"output artifact must declare derived_from: {artifact.id}")
        if any(item not in input_by_id for item in artifact.derived_from):
            raise ValueError(f"output artifact references an unknown input: {artifact.id}")
        source_roles = {input_by_id[item].role for item in artifact.derived_from}
        if not source_roles.issubset(set(output_contract.derived_from_roles)):
            raise ValueError(
                f"output artifact derivation violates its role contract: {artifact.id}"
            )
        output_counts[artifact.role] += 1
    for output_contract in operation.outputs:
        count = output_counts[output_contract.role]
        if count < output_contract.minimum or (
            output_contract.maximum is not None and count > output_contract.maximum
        ):
            raise ValueError(f"output role cardinality is invalid: {output_contract.role}")


def validate_status_against_request(
    status: TargetJobStatus,
    request: TargetJobRequest,
    operation: OperationContract,
) -> None:
    if status.job_id != request.job_id:
        raise ValueError("target status job ID does not match the accepted request")
    if status.attempt != request.attempt:
        raise ValueError("target status attempt does not match the accepted request")
    if status.request_sha256 != request.request_sha256:
        raise ValueError("target status request digest does not match the accepted request")
    if status.plan_sha256 != request.plan.plan_sha256:
        raise ValueError("target status plan digest does not match the accepted plan")
    evidence = status.execution_evidence
    if evidence is not None:
        if evidence.target.implementation_id != request.plan.target_implementation_id:
            raise ValueError("execution target identity does not match the accepted plan")
        if evidence.target.contract_sha256 != request.plan.target_contract_sha256:
            raise ValueError("execution target contract does not match the accepted plan")
        if evidence.operation.id != request.plan.operation_id:
            raise ValueError("execution operation does not match the accepted plan")
        if evidence.operation.contract_sha256 != request.plan.operation_contract_sha256:
            raise ValueError("execution operation contract does not match the accepted plan")
        if evidence.effective_intent != request.plan.effective_intent:
            raise ValueError("execution intent does not match the accepted plan")
        if evidence.effective_target_options != request.plan.effective_target_options:
            raise ValueError("execution target options do not match the accepted plan")
    if status.state == "succeeded":
        validate_artifacts_against_operation(
            operation,
            inputs=request.plan.inputs,
            outputs=status.outputs,
        )


def validate_preflight_response_against_request(
    response: TargetPreflightResponse,
    request: TargetPreflightRequest,
) -> None:
    """Require a target plan to preserve the complete declared transform."""

    plan = response.plan
    for field in (
        "operation_id",
        "operation_contract_sha256",
        "workspace_id",
        "inputs",
        "intent",
        "target_options",
    ):
        if getattr(plan, field) != getattr(request, field):
            raise ValueError(f"preflight plan changed declared {field}")


__all__ = [
    "ARTIFACT_ID_PATTERN",
    "REGISTRATION_ID_PATTERN",
    "SEMANTIC_ID_PATTERN",
    "SHA256_PATTERN",
    "SHARED_DIRECTORY_TRANSPORT",
    "TARGET_PROTOCOL",
    "WORKSPACE_ID_PATTERN",
    "Artifact",
    "ExecutionToolEvidence",
    "InputArtifactContract",
    "JsonSchemaDocument",
    "OperationContract",
    "OperationContractPayload",
    "OutputArtifactContract",
    "TargetCancelRequest",
    "TargetContract",
    "TargetContractPayload",
    "TargetExecutionEvidence",
    "TargetFailure",
    "TargetJobRequest",
    "TargetJobRequestPayload",
    "TargetJobState",
    "TargetJobStatus",
    "TargetOperationSupport",
    "TargetPreflightRequest",
    "TargetPreflightResponse",
    "TargetProgress",
    "TransformDeclaration",
    "TransformPlan",
    "TransformPlanPayload",
    "canonical_json_bytes",
    "canonical_json_sha256",
    "normalize_relative_posix_path",
    "validate_artifacts_against_operation",
    "validate_preflight_response_against_request",
    "validate_status_against_request",
]

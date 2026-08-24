"""Published hardware-neutral target protocols for stove0 operations."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)
from riverhog_protocol.collection_workflows import (
    CollectionDerivation,
)
from riverhog_protocol.collection_workflows import (
    canonical_json_sha256 as riverhog_canonical_json_sha256,
)
from stove0_protocol import (
    RIVERHOG_CAPABILITY_TRANSPORT,
    CollectionRootRef,
    ControllerEvidence,
    JsonSchemaDocument,
    OperationResultKind,
)
from stove0_protocol.jcs import canonical_json_bytes, canonical_json_sha256
from stove0_protocol.models import ObservationEvidence

TRANSFORM_TARGET_PROTOCOL: Literal["stove0-transform-target/v1"] = "stove0-transform-target/v1"
EFFECT_TARGET_PROTOCOL: Literal["stove0-effect-target/v1"] = "stove0-effect-target/v1"
EFFECT_RECEIPT_FORMAT: Literal["stove0-external-effect-receipt/v1"] = (
    "stove0-external-effect-receipt/v1"
)
MAXIMUM_EFFECT_RECEIPT_RESULT_BYTES = 64 * 1024
SHA256_PATTERN = r"^[0-9a-f]{64}$"
SEMANTIC_ID_PATTERN = r"^[a-z0-9](?:[a-z0-9._/-]{0,158}[a-z0-9])?$"
ARTIFACT_ID_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,158}[A-Za-z0-9])?$"

Sha256 = Annotated[str, StringConstraints(pattern=SHA256_PATTERN)]
SemanticId = Annotated[str, StringConstraints(pattern=SEMANTIC_ID_PATTERN)]
TargetJobState = Literal[
    "queued",
    "running",
    "canceling",
    "interrupted",
    "inapplicable",
    "succeeded",
    "failed",
    "canceled",
]
WorkspaceAssurance = Literal["encrypted", "ephemeral"]
InputDisposition = Literal["transformed", "preserved", "omitted", "rejected"]
TargetProtocol = Literal["stove0-transform-target/v1", "stove0-effect-target/v1"]
TargetResultKind = OperationResultKind


def _without_digest(model: BaseModel, field: str) -> dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True, exclude={field}, exclude_none=True)


class TargetProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class InputArtifactContract(TargetProtocolModel):
    role: SemanticId
    minimum: int = Field(default=1, ge=0)
    maximum: int | None = Field(default=None, ge=1)
    allowed_dispositions: tuple[InputDisposition, ...] | None = None

    @field_validator("allowed_dispositions")
    @classmethod
    def canonical_dispositions(
        cls,
        value: tuple[InputDisposition, ...] | None,
    ) -> tuple[InputDisposition, ...] | None:
        if value is None:
            return None
        if not value or value != tuple(sorted(value)) or len(value) != len(set(value)):
            raise ValueError("allowed input dispositions must be unique and ordered")
        return value

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
    def unique_roles(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(value)) or len(value) != len(set(value)):
            raise ValueError("derived_from_roles must be unique and ordered")
        return value

    @model_validator(mode="after")
    def validate_cardinality(self) -> Self:
        if self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("output artifact minimum must be <= maximum")
        return self


class OperationContractPayload(TargetProtocolModel):
    id: SemanticId
    result_kind: TargetResultKind = "collection"
    intent_schema: JsonSchemaDocument
    inputs: tuple[InputArtifactContract, ...] = Field(min_length=1)
    outputs: tuple[OutputArtifactContract, ...] = ()
    effect_receipt_schema: JsonSchemaDocument | None = None
    source_retirement_permitted: bool = False

    @model_validator(mode="after")
    def validate_roles(self) -> Self:
        input_roles = [item.role for item in self.inputs]
        output_roles = [item.role for item in self.outputs]
        if input_roles != sorted(input_roles) or len(input_roles) != len(set(input_roles)):
            raise ValueError("operation input roles must be unique and ordered")
        if output_roles != sorted(output_roles) or len(output_roles) != len(set(output_roles)):
            raise ValueError("operation output roles must be unique and ordered")
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
        if self.result_kind == "collection":
            if not self.outputs:
                raise ValueError("collection-producing operation requires output contracts")
            if self.effect_receipt_schema is not None:
                raise ValueError("collection-producing operation cannot declare an effect receipt")
            if any(item.allowed_dispositions is None for item in self.inputs):
                raise ValueError(
                    "collection-producing operation requires explicit input dispositions"
                )
        else:
            if self.outputs:
                raise ValueError("effect-producing operation cannot declare output artifacts")
            if self.effect_receipt_schema is None:
                raise ValueError("effect-producing operation requires a receipt schema")
            if self.source_retirement_permitted:
                raise ValueError("effect-producing operation cannot permit source retirement")
            if any(item.allowed_dispositions is not None for item in self.inputs):
                raise ValueError("effect-producing operation cannot declare input dispositions")
        return self


class OperationContract(OperationContractPayload):
    contract_sha256: Sha256

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        if canonical_json_sha256(_without_digest(self, "contract_sha256")) != self.contract_sha256:
            raise ValueError("operation contract digest does not match its canonical payload")
        return self

    @classmethod
    def seal(cls, payload: OperationContractPayload) -> OperationContract:
        document = payload.model_dump(mode="json", by_alias=True, exclude_none=True)
        return cls(**document, contract_sha256=canonical_json_sha256(document))


class TargetOperationSupport(TargetProtocolModel):
    operation_id: SemanticId
    operation_contract_sha256: Sha256
    result_kind: TargetResultKind = "collection"
    options_schema: JsonSchemaDocument


class TargetContractPayload(TargetProtocolModel):
    protocol: TargetProtocol = TRANSFORM_TARGET_PROTOCOL
    implementation_id: SemanticId
    implementation_version: str = Field(min_length=1, max_length=120)
    source_revision: str = Field(min_length=1, max_length=200)
    image_digest: Sha256
    transport: Literal["riverhog-capability/v1"] = RIVERHOG_CAPABILITY_TRANSPORT
    operations: tuple[TargetOperationSupport, ...] = Field(min_length=1)

    @field_validator("operations")
    @classmethod
    def canonical_operations(
        cls, value: tuple[TargetOperationSupport, ...]
    ) -> tuple[TargetOperationSupport, ...]:
        ids = [item.operation_id for item in value]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("target operations must be unique and ordered by operation ID")
        return value

    @model_validator(mode="after")
    def bind_result_kind(self) -> Self:
        expected = "collection" if self.protocol == TRANSFORM_TARGET_PROTOCOL else "external-effect"
        if any(item.result_kind != expected for item in self.operations):
            raise ValueError("target protocol and operation result kinds differ")
        return self


class TargetContract(TargetContractPayload):
    contract_sha256: Sha256

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        if canonical_json_sha256(_without_digest(self, "contract_sha256")) != self.contract_sha256:
            raise ValueError("target contract digest does not match its canonical payload")
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


class InputArtifact(TargetProtocolModel):
    id: str = Field(pattern=ARTIFACT_ID_PATTERN)
    role: SemanticId
    collection: CollectionRootRef
    path: str = Field(min_length=1, max_length=4096)
    bytes: int = Field(ge=0)
    sha256: Sha256
    media_type: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("path")
    @classmethod
    def canonical_path(cls, value: str) -> str:
        from riverhog_protocol.paths import normalize_relpath

        normalized = normalize_relpath(value)
        if normalized != value:
            raise ValueError("artifact path must be canonical")
        return value


class OutputArtifact(TargetProtocolModel):
    id: str = Field(pattern=ARTIFACT_ID_PATTERN)
    role: SemanticId
    path: str = Field(min_length=1, max_length=4096)
    bytes: int = Field(ge=0)
    sha256: Sha256
    media_type: str | None = Field(default=None, min_length=1, max_length=255)
    derived_from: tuple[str, ...] = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def canonical_path(cls, value: str) -> str:
        from riverhog_protocol.paths import normalize_relpath

        normalized = normalize_relpath(value)
        if normalized != value:
            raise ValueError("artifact path must be canonical")
        return value

    @field_validator("derived_from")
    @classmethod
    def canonical_sources(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(value)) or len(value) != len(set(value)):
            raise ValueError("derived_from artifact IDs must be unique and ordered")
        if any(re.fullmatch(ARTIFACT_ID_PATTERN, item) is None for item in value):
            raise ValueError("derived_from contains an invalid artifact ID")
        return value


class TargetDeclaration(TargetProtocolModel):
    operation_id: SemanticId
    operation_contract_sha256: Sha256
    inputs: tuple[InputArtifact, ...] = Field(min_length=1)
    intent: dict[str, JsonValue]
    target_options: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("inputs")
    @classmethod
    def canonical_inputs(cls, value: tuple[InputArtifact, ...]) -> tuple[InputArtifact, ...]:
        ids = [item.id for item in value]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("target inputs must be unique and ordered by artifact ID")
        identities = [(item.collection.collection_id, item.path) for item in value]
        if len(identities) != len(set(identities)):
            raise ValueError("target inputs must not repeat a collection artifact")
        return value


class TargetPreflightRequest(TargetDeclaration):
    protocol: TargetProtocol = TRANSFORM_TARGET_PROTOCOL
    observations: tuple[ObservationEvidence, ...] = ()

    @field_validator("observations")
    @classmethod
    def canonical_observations(
        cls,
        value: tuple[ObservationEvidence, ...],
    ) -> tuple[ObservationEvidence, ...]:
        ids = [item.request.request_id for item in value]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("target preflight observations must be unique and ordered")
        return value


class TransformPlanPayload(TargetDeclaration):
    protocol: Literal["stove0-transform-target/v1"] = TRANSFORM_TARGET_PROTOCOL
    target_implementation_id: SemanticId
    target_contract_sha256: Sha256
    observation_result_sha256s: tuple[Sha256, ...] = ()

    @field_validator("observation_result_sha256s")
    @classmethod
    def canonical_observation_results(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(value)) or len(value) != len(set(value)):
            raise ValueError("target plan observation results must be unique and ordered")
        return value


class TransformPlan(TransformPlanPayload):
    plan_sha256: Sha256

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        if canonical_json_sha256(_without_digest(self, "plan_sha256")) != self.plan_sha256:
            raise ValueError("target plan digest does not match its canonical payload")
        return self

    @classmethod
    def seal(cls, payload: TransformPlanPayload) -> TransformPlan:
        document = payload.model_dump(mode="json", by_alias=True, exclude_none=True)
        return cls(**document, plan_sha256=canonical_json_sha256(document))

    def binding_document(self) -> dict[str, JsonValue]:
        return self.model_dump(
            mode="json",
            by_alias=True,
            exclude={"plan_sha256"},
            exclude_none=True,
        )


class EffectPlanPayload(TargetDeclaration):
    protocol: Literal["stove0-effect-target/v1"] = EFFECT_TARGET_PROTOCOL
    target_implementation_id: SemanticId
    target_contract_sha256: Sha256
    observation_result_sha256s: tuple[Sha256, ...] = ()

    @field_validator("observation_result_sha256s")
    @classmethod
    def canonical_observation_results(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(value)) or len(value) != len(set(value)):
            raise ValueError("target plan observation results must be unique and ordered")
        return value


class EffectPlan(EffectPlanPayload):
    plan_sha256: Sha256

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        if canonical_json_sha256(_without_digest(self, "plan_sha256")) != self.plan_sha256:
            raise ValueError("target plan digest does not match its canonical payload")
        return self

    @classmethod
    def seal(cls, payload: EffectPlanPayload) -> EffectPlan:
        document = payload.model_dump(mode="json", by_alias=True, exclude_none=True)
        return cls(**document, plan_sha256=canonical_json_sha256(document))

    def binding_document(self) -> dict[str, JsonValue]:
        return self.model_dump(
            mode="json",
            by_alias=True,
            exclude={"plan_sha256"},
            exclude_none=True,
        )


TargetPlan = Annotated[TransformPlan | EffectPlan, Field(discriminator="protocol")]


class TargetPreflightResponse(TargetProtocolModel):
    target: TargetContract
    plan: TargetPlan

    @model_validator(mode="after")
    def bind_protocol(self) -> Self:
        if self.target.protocol != self.plan.protocol:
            raise ValueError("target contract and preflight plan protocols differ")
        return self


class TargetRuntimeAuthority(TargetProtocolModel):
    transport: Literal["riverhog-capability/v1"] = RIVERHOG_CAPABILITY_TRANSPORT
    riverhog_base_url: str = Field(min_length=1, max_length=2048)
    capability_token: str = Field(min_length=1, max_length=4096, repr=False)
    allow_insecure_http: bool = False


class TargetJobDeclaration(TargetProtocolModel):
    job_id: Sha256
    claim_id: str = Field(min_length=1, max_length=160)
    fence: int = Field(ge=1)
    controller_evidence: ControllerEvidence
    plan: TargetPlan
    workspace_assurance: WorkspaceAssurance

    @field_validator("claim_id")
    @classmethod
    def canonical_claim_id(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("claim id must be canonical")
        return value

    @model_validator(mode="after")
    def bind_execution(self) -> Self:
        envelope = self.controller_evidence.execution_envelope
        workflow = envelope.workflow_plan
        target = envelope.target_plan
        if self.job_id != envelope.execution_envelope_sha256:
            raise ValueError("target job id must equal the execution envelope identity")
        if self.claim_id != envelope.claim_id or self.fence != envelope.fence:
            raise ValueError("target job claim differs from the execution envelope")
        if self.plan.plan_sha256 != target.plan_sha256:
            raise ValueError("target job plan differs from the sealed execution envelope")
        if self.plan.binding_document() != target.plan:
            raise ValueError("target job plan document differs from the sealed binding")
        if (
            self.plan.target_contract_sha256 != workflow.target_contract_sha256
            or self.plan.operation_contract_sha256 != workflow.operation.sha256
            or self.plan.observation_result_sha256s
            != tuple(sorted(item.result.result_sha256 for item in workflow.observations))
        ):
            raise ValueError("target job plan differs from the stove0 workflow plan")
        work_roots = {
            (item.collection_id, item.archive_root_sha256, item.content_identity)
            for item in workflow.work.inputs
        }
        if any(
            (
                item.collection.collection_id,
                item.collection.archive_root_sha256,
                item.collection.content_identity,
            )
            not in work_roots
            for item in self.plan.inputs
        ):
            raise ValueError("target plan references an input outside the stove0 work")
        return self


class AcceptedTargetJob(TargetProtocolModel):
    """Durable, non-secret identity of one accepted target job request."""

    declaration: TargetJobDeclaration
    request_sha256: Sha256

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        declaration = self.declaration.model_dump(mode="json", by_alias=True, exclude_none=True)
        if canonical_json_sha256(declaration) != self.request_sha256:
            raise ValueError("accepted target job digest does not bind its declaration")
        return self


class TargetJobRequest(TargetProtocolModel):
    """Secret-bearing target invocation; never store this document durably."""

    declaration: TargetJobDeclaration
    runtime: TargetRuntimeAuthority
    request_sha256: Sha256

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        declaration = self.declaration.model_dump(mode="json", by_alias=True, exclude_none=True)
        if canonical_json_sha256(declaration) != self.request_sha256:
            raise ValueError("target job request digest must bind the non-secret declaration")
        return self

    @classmethod
    def seal(
        cls,
        declaration: TargetJobDeclaration,
        runtime: TargetRuntimeAuthority,
    ) -> TargetJobRequest:
        document = declaration.model_dump(mode="json", by_alias=True, exclude_none=True)
        return cls(
            declaration=declaration,
            runtime=runtime,
            request_sha256=canonical_json_sha256(document),
        )

    def accepted(self) -> AcceptedTargetJob:
        return AcceptedTargetJob(
            declaration=self.declaration,
            request_sha256=self.request_sha256,
        )


class TargetProgress(TargetProtocolModel):
    phase: str = Field(min_length=1, max_length=120)
    completed: int = Field(ge=0)
    total: int | None = Field(default=None, ge=0)
    unit: str | None = Field(default=None, min_length=1, max_length=40)

    @model_validator(mode="after")
    def validate_total(self) -> Self:
        if self.total is not None and self.completed > self.total:
            raise ValueError("target progress completed exceeds total")
        return self


class TargetFailure(TargetProtocolModel):
    code: SemanticId
    message: str = Field(min_length=1, max_length=1000)
    retryable: bool


class TargetInapplicable(TargetProtocolModel):
    code: SemanticId
    message: str = Field(min_length=1, max_length=1000)


class TargetExecutionEvidence(TargetProtocolModel):
    target_contract_sha256: Sha256
    operation_contract_sha256: Sha256
    plan_sha256: Sha256
    execution_sha256: Sha256
    runtime: dict[str, JsonValue] = Field(default_factory=dict)


class OutputCollectionRef(TargetProtocolModel):
    collection_id: int = Field(ge=1)
    archive_root_sha256: Sha256
    content_identity: Sha256
    derivation_sha256: Sha256


class ExternalEffectReceiptPayload(TargetProtocolModel):
    format: Literal["stove0-external-effect-receipt/v1"] = EFFECT_RECEIPT_FORMAT
    job_id: Sha256
    request_sha256: Sha256
    target_contract_sha256: Sha256
    operation_contract_sha256: Sha256
    plan_sha256: Sha256
    execution_sha256: Sha256
    result: dict[str, JsonValue]

    @model_validator(mode="after")
    def bounded_result(self) -> Self:
        if len(canonical_json_bytes(self.result)) > MAXIMUM_EFFECT_RECEIPT_RESULT_BYTES:
            raise ValueError("external-effect receipt result exceeds its bounded size")
        return self


class ExternalEffectReceipt(ExternalEffectReceiptPayload):
    receipt_sha256: Sha256

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        if canonical_json_sha256(_without_digest(self, "receipt_sha256")) != self.receipt_sha256:
            raise ValueError("external-effect receipt digest does not match its canonical payload")
        return self

    @classmethod
    def seal(cls, payload: ExternalEffectReceiptPayload) -> ExternalEffectReceipt:
        document = payload.model_dump(mode="json", by_alias=True, exclude_none=True)
        return cls(**document, receipt_sha256=canonical_json_sha256(document))


class TargetJobStatus(TargetProtocolModel):
    protocol: TargetProtocol = TRANSFORM_TARGET_PROTOCOL
    job_id: Sha256
    state: TargetJobState
    attempt: int = Field(ge=1)
    request_sha256: Sha256
    plan_sha256: Sha256
    progress: TargetProgress
    outputs: tuple[OutputArtifact, ...] = ()
    output_collection: OutputCollectionRef | None = None
    execution_evidence: TargetExecutionEvidence | None = None
    derivation: dict[str, Any] | None = None
    effect_receipt: ExternalEffectReceipt | None = None
    failure: TargetFailure | None = None
    inapplicable: TargetInapplicable | None = None

    @field_validator("outputs")
    @classmethod
    def canonical_outputs(cls, value: tuple[OutputArtifact, ...]) -> tuple[OutputArtifact, ...]:
        ids = [item.id for item in value]
        paths = [item.path for item in value]
        if ids != sorted(ids) or len(ids) != len(set(ids)) or len(paths) != len(set(paths)):
            raise ValueError("target outputs must have unique IDs/paths and be ordered by ID")
        return value

    @field_validator("derivation")
    @classmethod
    def canonical_derivation(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        return CollectionDerivation.from_mapping(value).as_dict()

    @model_validator(mode="after")
    def validate_terminal_shape(self) -> Self:
        if self.state == "succeeded":
            if self.protocol == TRANSFORM_TARGET_PROTOCOL:
                if (
                    not self.outputs
                    or self.output_collection is None
                    or self.execution_evidence is None
                    or self.derivation is None
                ):
                    raise ValueError(
                        "succeeded transform status requires outputs and final collection evidence"
                    )
                if self.effect_receipt is not None:
                    raise ValueError("succeeded transform status cannot include an effect receipt")
            elif (
                self.outputs
                or self.output_collection is not None
                or self.derivation is not None
                or self.execution_evidence is None
                or self.effect_receipt is None
            ):
                raise ValueError(
                    "succeeded effect status requires only execution evidence and an effect receipt"
                )
            if self.failure is not None:
                raise ValueError("succeeded target status cannot include failure details")
            if self.inapplicable is not None:
                raise ValueError("succeeded target status cannot be inapplicable")
        elif self.state == "failed":
            if self.failure is None:
                raise ValueError("failed target status requires failure details")
            if (
                self.outputs
                or self.output_collection is not None
                or self.derivation is not None
                or self.effect_receipt is not None
            ):
                raise ValueError("failed target status cannot publish a result")
            if self.inapplicable is not None:
                raise ValueError("failed target status cannot be inapplicable")
        elif self.state == "inapplicable":
            if self.inapplicable is None:
                raise ValueError("inapplicable target status requires an outcome")
            if (
                self.outputs
                or self.output_collection is not None
                or self.derivation is not None
                or self.effect_receipt is not None
            ):
                raise ValueError("inapplicable target status cannot publish a result")
            if self.failure is not None:
                raise ValueError("inapplicable target status cannot include failure details")
        elif self.state == "canceled":
            if (
                self.outputs
                or self.output_collection is not None
                or self.derivation is not None
                or self.effect_receipt is not None
            ):
                raise ValueError("canceled target status cannot publish a result")
        elif (
            self.outputs
            or self.output_collection is not None
            or self.execution_evidence is not None
            or self.derivation is not None
            or self.effect_receipt is not None
        ):
            raise ValueError("nonterminal target status cannot publish terminal evidence")
        if self.state != "inapplicable" and self.inapplicable is not None:
            raise ValueError("only inapplicable target status may include an outcome")
        return self


def validate_preflight_response_against_request(
    response: TargetPreflightResponse,
    request: TargetPreflightRequest,
) -> None:
    support = response.target.support_for(request.operation_id)
    if (
        response.target.protocol != request.protocol
        or response.plan.protocol != request.protocol
        or support.operation_contract_sha256 != request.operation_contract_sha256
    ):
        raise ValueError("target advertised a different operation contract")
    plan = response.plan
    if (
        plan.target_implementation_id != response.target.implementation_id
        or plan.target_contract_sha256 != response.target.contract_sha256
        or plan.operation_id != request.operation_id
        or plan.operation_contract_sha256 != request.operation_contract_sha256
        or plan.inputs != request.inputs
        or plan.intent != request.intent
        or plan.observation_result_sha256s
        != tuple(sorted(item.result.result_sha256 for item in request.observations))
        or any(
            key not in plan.target_options or plan.target_options[key] != value
            for key, value in request.target_options.items()
        )
    ):
        raise ValueError("target preflight plan differs from the request or target contract")


def validate_declaration_against_operation(
    declaration: TargetDeclaration,
    operation: OperationContract,
) -> None:
    if (
        declaration.operation_id != operation.id
        or declaration.operation_contract_sha256 != operation.contract_sha256
    ):
        raise ValueError("target declaration does not bind the operation contract")
    counts: dict[str, int] = {}
    for artifact in declaration.inputs:
        counts[artifact.role] = counts.get(artifact.role, 0) + 1
    expected_roles = {item.role for item in operation.inputs}
    if set(counts) - expected_roles:
        raise ValueError("target declaration includes an unsupported input role")
    for contract in operation.inputs:
        count = counts.get(contract.role, 0)
        if count < contract.minimum or (contract.maximum is not None and count > contract.maximum):
            raise ValueError(f"input role cardinality is invalid: {contract.role}")


def validate_status_against_request(
    status: TargetJobStatus,
    request: TargetJobRequest | AcceptedTargetJob,
    operation: OperationContract,
) -> None:
    declaration = request.declaration
    if (
        status.job_id != declaration.job_id
        or status.protocol != declaration.plan.protocol
        or status.request_sha256 != request.request_sha256
        or status.plan_sha256 != declaration.plan.plan_sha256
    ):
        raise ValueError("target status does not bind the accepted request")
    expected_kind = (
        "collection"
        if declaration.plan.protocol == TRANSFORM_TARGET_PROTOCOL
        else "external-effect"
    )
    if operation.result_kind != expected_kind:
        raise ValueError("target plan protocol differs from the operation result kind")
    if status.state != "succeeded":
        return
    evidence = status.execution_evidence
    if evidence is None or (
        evidence.target_contract_sha256 != declaration.plan.target_contract_sha256
        or evidence.operation_contract_sha256 != operation.contract_sha256
        or evidence.plan_sha256 != declaration.plan.plan_sha256
    ):
        raise ValueError("target execution evidence differs from the accepted plan")
    if operation.result_kind == "external-effect":
        receipt = status.effect_receipt
        if receipt is None or (
            receipt.job_id != declaration.job_id
            or receipt.request_sha256 != request.request_sha256
            or receipt.target_contract_sha256 != declaration.plan.target_contract_sha256
            or receipt.operation_contract_sha256 != operation.contract_sha256
            or receipt.plan_sha256 != declaration.plan.plan_sha256
            or receipt.execution_sha256 != evidence.execution_sha256
        ):
            raise ValueError("external-effect receipt differs from the accepted execution")
        if operation.effect_receipt_schema is None:
            raise ValueError("effect operation omitted its receipt schema")
        from jsonschema import Draft202012Validator

        Draft202012Validator(operation.effect_receipt_schema.document).validate(receipt.result)
        return
    input_by_id = {item.id: item for item in declaration.plan.inputs}
    counts: dict[str, int] = {}
    contract_by_role = {item.role: item for item in operation.outputs}
    for output in status.outputs:
        contract = contract_by_role.get(output.role)
        if contract is None:
            raise ValueError("target produced an unsupported output role")
        counts[output.role] = counts.get(output.role, 0) + 1
        if any(source not in input_by_id for source in output.derived_from):
            raise ValueError("target output derives from an unknown input artifact")
        source_roles = {input_by_id[source].role for source in output.derived_from}
        if not source_roles.issubset(set(contract.derived_from_roles)):
            raise ValueError("target output derivation violates the operation contract")
    for contract in operation.outputs:
        count = counts.get(contract.role, 0)
        if count < contract.minimum or (contract.maximum is not None and count > contract.maximum):
            raise ValueError(f"output role cardinality is invalid: {contract.role}")
    derivation = (
        CollectionDerivation.from_mapping(status.derivation)
        if status.derivation is not None
        else None
    )
    output_collection = status.output_collection
    if derivation is None or output_collection is None:
        raise ValueError("target success is missing derivation evidence")
    workflow = declaration.controller_evidence.execution_envelope.workflow_plan
    controller_document = declaration.controller_evidence.model_dump(
        mode="json",
        by_alias=True,
        exclude_none=True,
    )
    expected_inputs = tuple(item.to_identity() for item in workflow.work.inputs)
    if (
        derivation.execution_id != declaration.job_id
        or derivation.claim_id != declaration.claim_id
        or derivation.fence != declaration.fence
        or derivation.recipe != workflow.work.recipe.to_identity()
        or derivation.operation != workflow.operation.to_identity()
        or derivation.inputs != expected_inputs
        or derivation.output_tags != workflow.output_tags
        or derivation.execution_envelope_sha256 != declaration.job_id
        or derivation.execution_sha256 != evidence.execution_sha256
        or derivation.controller_evidence != controller_document
        or derivation.controller_evidence_sha256
        != riverhog_canonical_json_sha256(controller_document)
        or output_collection.derivation_sha256 != derivation.sha256
    ):
        raise ValueError("target derivation differs from the accepted execution envelope")
    planned_inputs = {
        (item.collection.collection_id, item.collection.archive_root_sha256, item.path)
        for item in declaration.plan.inputs
    }
    disposition_inputs = {
        (
            item.input_collection_id,
            item.input_archive_root_sha256,
            item.input_path,
        )
        for item in derivation.dispositions
    }
    if disposition_inputs != planned_inputs:
        raise ValueError("target derivation does not account for every planned input artifact")
    input_contracts = {item.role: item for item in operation.inputs}
    planned_by_identity = {
        (item.collection.collection_id, item.collection.archive_root_sha256, item.path): item
        for item in declaration.plan.inputs
    }
    for disposition in derivation.dispositions:
        identity = (
            disposition.input_collection_id,
            disposition.input_archive_root_sha256,
            disposition.input_path,
        )
        planned = planned_by_identity[identity]
        allowed_dispositions = input_contracts[planned.role].allowed_dispositions
        if allowed_dispositions is None or disposition.status not in allowed_dispositions:
            raise ValueError(
                "target disposition is not permitted for input role: "
                f"{planned.role}: {disposition.status}"
            )
    output_paths = {item.path for item in status.outputs}
    disposition_outputs = {path for item in derivation.dispositions for path in item.outputs}
    if disposition_outputs != output_paths:
        raise ValueError("target derivation output paths differ from terminal target outputs")


__all__ = [
    "ARTIFACT_ID_PATTERN",
    "AcceptedTargetJob",
    "EFFECT_RECEIPT_FORMAT",
    "EFFECT_TARGET_PROTOCOL",
    "EffectPlan",
    "EffectPlanPayload",
    "ExternalEffectReceipt",
    "ExternalEffectReceiptPayload",
    "InputArtifact",
    "InputDisposition",
    "InputArtifactContract",
    "OperationContract",
    "OperationContractPayload",
    "OutputArtifact",
    "OutputArtifactContract",
    "OutputCollectionRef",
    "SHA256_PATTERN",
    "SemanticId",
    "Sha256",
    "TRANSFORM_TARGET_PROTOCOL",
    "TargetContract",
    "TargetContractPayload",
    "TargetExecutionEvidence",
    "TargetFailure",
    "TargetInapplicable",
    "TargetJobDeclaration",
    "TargetJobRequest",
    "TargetJobState",
    "TargetJobStatus",
    "TargetOperationSupport",
    "TargetPlan",
    "TargetPreflightRequest",
    "TargetPreflightResponse",
    "TargetProgress",
    "TargetProtocolModel",
    "TargetProtocol",
    "TargetResultKind",
    "TargetRuntimeAuthority",
    "TargetDeclaration",
    "TransformPlan",
    "TransformPlanPayload",
    "WorkspaceAssurance",
    "validate_declaration_against_operation",
    "validate_preflight_response_against_request",
    "validate_status_against_request",
]

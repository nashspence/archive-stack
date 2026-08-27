"""Canonical contracts for stove0 work, observations, plans, and evidence.

The package is intentionally independent of stove0 implementation code. External
observer and target authors may depend on these models without importing the
orchestrator, cloning the Riverhog repository, or sharing a database schema.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)
from referencing import Registry
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012
from riverhog_protocol.collection_workflows import (
    CollectionRootIdentity,
    OperationIdentity,
    RecipeIdentity,
)
from riverhog_protocol.paths import CollectionId, normalize_relpath, normalize_tag

from stove0_protocol.jcs import canonical_json_bytes, canonical_json_sha256

WORK_FORMAT: Literal["stove0-work/v1"] = "stove0-work/v1"
OBSERVER_PROTOCOL: Literal["stove0-content-observer/v1"] = "stove0-content-observer/v1"
OBSERVATION_REQUEST_FORMAT: Literal["stove0-observation-request/v1"] = (
    "stove0-observation-request/v1"
)
OBSERVATION_RESULT_FORMAT: Literal["stove0-observation-result/v1"] = "stove0-observation-result/v1"
WORKFLOW_PLAN_FORMAT: Literal["stove0-workflow-plan/v1"] = "stove0-workflow-plan/v1"
EXECUTION_ENVELOPE_FORMAT: Literal["stove0-execution-envelope/v1"] = "stove0-execution-envelope/v1"
CONTROLLER_EVIDENCE_FORMAT: Literal["stove0-controller-evidence/v1"] = (
    "stove0-controller-evidence/v1"
)
WORKFLOW_PREVIEW_REQUEST_FORMAT: Literal["stove0-workflow-preview-request/v1"] = (
    "stove0-workflow-preview-request/v1"
)
WORKFLOW_PREVIEW_FORMAT: Literal["stove0-workflow-preview/v1"] = "stove0-workflow-preview/v1"
EVALUATION_MATRIX_FORMAT: Literal["stove0-evaluation-matrix/v1"] = "stove0-evaluation-matrix/v1"
EVALUATION_DEFINITION_FORMAT: Literal["stove0-evaluation-definition/v1"] = (
    "stove0-evaluation-definition/v1"
)
RIVERHOG_CAPABILITY_TRANSPORT: Literal["riverhog-capability/v1"] = "riverhog-capability/v1"

SHA256_PATTERN = r"^[0-9a-f]{64}$"
SEMANTIC_ID_PATTERN = r"^[a-z0-9](?:[a-z0-9._/-]{0,158}[a-z0-9])?$"
ARTIFACT_ID_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,158}[A-Za-z0-9])?$"
REGISTRATION_ID_PATTERN = r"^[a-z0-9](?:[a-z0-9.-]{0,118}[a-z0-9])?$"

Sha256 = Annotated[str, StringConstraints(pattern=SHA256_PATTERN)]
SemanticId = Annotated[str, StringConstraints(pattern=SEMANTIC_ID_PATTERN)]
RegistrationId = Annotated[str, StringConstraints(pattern=REGISTRATION_ID_PATTERN)]
ObservationState = Literal["observed", "inapplicable", "failed", "canceled"]
RetirementPolicy = Literal["retain", "retire-after-verified-output"]
RetrievalPolicy = Literal["available-only", "allow"]
OperationResultKind = Literal["collection", "external-effect"]


def _without_digest(model: BaseModel, field: str) -> dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True, exclude={field}, exclude_none=True)


def _root_payload(root: CollectionRootIdentity) -> dict[str, object]:
    return root.as_dict()


def _recipe_payload(recipe: RecipeIdentity) -> dict[str, object]:
    return recipe.as_dict()


def _operation_payload(operation: OperationIdentity) -> dict[str, object]:
    return operation.as_dict()


def _validate_local_schema_reference_closure(document: dict[str, JsonValue]) -> None:
    resource = DRAFT202012.create_resource(document)
    identifier = document.get("$id")
    base_uri = (
        identifier
        if isinstance(identifier, str)
        else f"urn:stove0:json-schema:{canonical_json_sha256(document)}"
    )
    resolver = Registry().with_resource(base_uri, resource).resolver(base_uri)

    def visit(contents: JsonValue, current_resolver: Any) -> None:
        if isinstance(contents, dict):
            for keyword in ("$ref", "$dynamicRef"):
                reference = contents.get(keyword)
                if reference is None:
                    continue
                if not isinstance(reference, str) or not reference.startswith("#"):
                    raise ValueError(
                        "schema references must resolve within the sealed schema document"
                    )
                try:
                    current_resolver.lookup(reference)
                except Unresolvable as exc:
                    raise ValueError(
                        "schema reference does not resolve within the sealed schema document"
                    ) from exc
        for subcontents in DRAFT202012.subresources_of(contents):
            subresource = DRAFT202012.create_resource(subcontents)
            visit(subcontents, current_resolver.in_subresource(subresource))

    visit(document, resolver)


class Stove0ProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class JsonSchemaDocument(Stove0ProtocolModel):
    id: SemanticId
    sha256: Sha256
    document: dict[str, JsonValue] = Field(alias="schema")

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        if canonical_json_sha256(self.document) != self.sha256:
            raise ValueError("schema sha256 does not match its canonical JSON")
        try:
            Draft202012Validator.check_schema(self.document)
        except SchemaError as exc:
            raise ValueError("schema is not valid JSON Schema Draft 2020-12") from exc
        _validate_local_schema_reference_closure(self.document)
        return self

    @classmethod
    def from_schema(cls, schema_id: str, schema: dict[str, JsonValue]) -> JsonSchemaDocument:
        return cls(id=schema_id, sha256=canonical_json_sha256(schema), schema=schema)


class CollectionRootRef(Stove0ProtocolModel):
    collection_id: CollectionId
    archive_root_sha256: Sha256
    content_identity: Sha256

    @classmethod
    def from_identity(cls, value: CollectionRootIdentity) -> CollectionRootRef:
        return cls(
            collection_id=value.collection_id,
            archive_root_sha256=value.archive_root_sha256,
            content_identity=value.content_identity,
        )

    def to_identity(self) -> CollectionRootIdentity:
        return CollectionRootIdentity(**self.model_dump(mode="python"))


class RecipeRef(Stove0ProtocolModel):
    id: SemanticId
    revision: int = Field(ge=1)
    sha256: Sha256

    @classmethod
    def from_identity(cls, value: RecipeIdentity) -> RecipeRef:
        return cls(id=value.id, revision=value.revision, sha256=value.sha256)

    def to_identity(self) -> RecipeIdentity:
        return RecipeIdentity(**self.model_dump(mode="python"))


class OperationRef(Stove0ProtocolModel):
    id: SemanticId
    sha256: Sha256

    @classmethod
    def from_identity(cls, value: OperationIdentity) -> OperationRef:
        return cls(id=value.id, sha256=value.sha256)

    def to_identity(self) -> OperationIdentity:
        return OperationIdentity(**self.model_dump(mode="python"))


class ArtifactSubject(Stove0ProtocolModel):
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
        normalized = normalize_relpath(value)
        if normalized != value:
            raise ValueError("artifact path must be canonical")
        return normalized


class BranchWorkBinding(Stove0ProtocolModel):
    """Stable parent/branch lineage for one ordinary child work identity."""

    kind: Literal["branch"] = "branch"
    parent_work_id: Sha256
    branch_id: SemanticId
    decision_sha256: Sha256
    artifact_selection_sha256: Sha256


class JoinWorkMemberBinding(Stove0ProtocolModel):
    """Exact successful branch result used to derive one join work identity."""

    branch_id: SemanticId
    settlement_sha256: Sha256
    producer_settlement_sha256: Sha256 | None = None
    artifact_selection_sha256: Sha256


class JoinWorkBinding(Stove0ProtocolModel):
    """Stable branch-set lineage for one ordinary join work identity."""

    kind: Literal["join"] = "join"
    parent_work_id: Sha256
    branch_set_sha256: Sha256
    members: tuple[JoinWorkMemberBinding, ...] = Field(min_length=2)

    @field_validator("members")
    @classmethod
    def canonical_members(
        cls, value: tuple[JoinWorkMemberBinding, ...]
    ) -> tuple[JoinWorkMemberBinding, ...]:
        branch_ids = [item.branch_id for item in value]
        if branch_ids != sorted(branch_ids) or len(branch_ids) != len(set(branch_ids)):
            raise ValueError("join work members must be unique and ordered by branch ID")
        return value


ForkJoinBinding = Annotated[
    BranchWorkBinding | JoinWorkBinding,
    Field(discriminator="kind"),
]


class EvaluationBinding(Stove0ProtocolModel):
    """Immutable membership of one work item in a trial/evaluation matrix."""

    evaluation_id: Sha256
    matrix_sha256: Sha256
    variant_id: SemanticId
    parameters: dict[str, JsonValue] = Field(default_factory=dict)


class WorkPayload(Stove0ProtocolModel):
    format: Literal["stove0-work/v1"] = WORK_FORMAT
    recipe: RecipeRef
    inputs: tuple[CollectionRootRef, ...] = Field(min_length=1)
    effective_intent: dict[str, JsonValue] = Field(default_factory=dict)
    evaluation: EvaluationBinding | None = None
    fork_join: ForkJoinBinding | None = None

    @field_validator("inputs")
    @classmethod
    def canonical_inputs(
        cls, value: tuple[CollectionRootRef, ...]
    ) -> tuple[CollectionRootRef, ...]:
        ordered = tuple(
            sorted(value, key=lambda item: (item.collection_id, item.archive_root_sha256))
        )
        if value != ordered or len(value) != len({item.collection_id for item in value}):
            raise ValueError("work inputs must be unique and canonically ordered")
        return value


class WorkIdentity(WorkPayload):
    work_id: Sha256

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        if canonical_json_sha256(_without_digest(self, "work_id")) != self.work_id:
            raise ValueError("work id does not match the canonical work payload")
        return self

    @classmethod
    def seal(cls, payload: WorkPayload) -> WorkIdentity:
        document = payload.model_dump(mode="json", by_alias=True, exclude_none=True)
        return cls(**document, work_id=canonical_json_sha256(document))

    def root_identities(self) -> tuple[CollectionRootIdentity, ...]:
        return tuple(item.to_identity() for item in self.inputs)


class SemanticValidationProfilePayload(Stove0ProtocolModel):
    """Portable identity for semantic rules not expressible by JSON Schema."""

    id: SemanticId
    rules: tuple[SemanticId, ...] = Field(min_length=1)
    conformance_vectors_sha256: Sha256 | None = None

    @field_validator("rules")
    @classmethod
    def canonical_rules(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(value)) or len(value) != len(set(value)):
            raise ValueError("semantic validation rules must be unique and ordered")
        return value


class SemanticValidationProfile(SemanticValidationProfilePayload):
    profile_sha256: Sha256

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        if canonical_json_sha256(_without_digest(self, "profile_sha256")) != self.profile_sha256:
            raise ValueError("semantic validation profile digest differs from its payload")
        return self

    @classmethod
    def seal(
        cls,
        payload: SemanticValidationProfilePayload,
    ) -> SemanticValidationProfile:
        document = payload.model_dump(mode="json", by_alias=True, exclude_none=True)
        return cls(**document, profile_sha256=canonical_json_sha256(document))


JSON_SCHEMA_ONLY_SEMANTIC_PROFILE = SemanticValidationProfile.seal(
    SemanticValidationProfilePayload(
        id="stove0.semantic.json-schema-only/v1",
        rules=("stove0.semantic.json-schema-draft-2020-12/v1",),
    )
)


class ObserverContractPayload(Stove0ProtocolModel):
    id: SemanticId
    options_schema: JsonSchemaDocument
    facts_schema: JsonSchemaDocument
    facts_semantics: SemanticValidationProfile
    maximum_result_bytes: int = Field(default=1024 * 1024, ge=1, le=64 * 1024 * 1024)

    @model_validator(mode="after")
    def bind_semantic_conformance_vectors(self) -> Self:
        if (
            self.facts_semantics != JSON_SCHEMA_ONLY_SEMANTIC_PROFILE
            and self.facts_semantics.conformance_vectors_sha256 is None
        ):
            raise ValueError(
                "non-schema-only observer semantics require conformance-vector identity"
            )
        return self


class ObserverContract(ObserverContractPayload):
    contract_sha256: Sha256

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        if canonical_json_sha256(_without_digest(self, "contract_sha256")) != self.contract_sha256:
            raise ValueError("observer contract digest does not match its canonical payload")
        return self

    @classmethod
    def seal(cls, payload: ObserverContractPayload) -> ObserverContract:
        document = payload.model_dump(mode="json", by_alias=True, exclude_none=True)
        return cls(**document, contract_sha256=canonical_json_sha256(document))


class ObserverContractSupport(Stove0ProtocolModel):
    contract_id: SemanticId
    contract_sha256: Sha256
    options_schema: JsonSchemaDocument
    facts_schema: JsonSchemaDocument
    facts_semantics: SemanticValidationProfile
    preferred_subject_batch_size: int = Field(default=128, ge=1)
    maximum_result_bytes: int = Field(ge=1, le=64 * 1024 * 1024)

    @classmethod
    def from_contract(
        cls,
        value: ObserverContract,
        *,
        preferred_subject_batch_size: int = 128,
    ) -> ObserverContractSupport:
        return cls(
            contract_id=value.id,
            contract_sha256=value.contract_sha256,
            options_schema=value.options_schema,
            facts_schema=value.facts_schema,
            facts_semantics=value.facts_semantics,
            preferred_subject_batch_size=preferred_subject_batch_size,
            maximum_result_bytes=value.maximum_result_bytes,
        )


class ObserverDescriptorPayload(Stove0ProtocolModel):
    protocol: Literal["stove0-content-observer/v1"] = OBSERVER_PROTOCOL
    implementation_id: SemanticId
    implementation_version: str = Field(min_length=1, max_length=120)
    source_revision: str = Field(min_length=1, max_length=200)
    image_digest: Sha256
    contracts: tuple[ObserverContractSupport, ...] = Field(min_length=1)

    @field_validator("contracts")
    @classmethod
    def unique_contracts(
        cls, value: tuple[ObserverContractSupport, ...]
    ) -> tuple[ObserverContractSupport, ...]:
        ids = [item.contract_id for item in value]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("observer contracts must be unique and ordered by ID")
        return value


class ObserverDescriptor(ObserverDescriptorPayload):
    descriptor_sha256: Sha256

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        expected = canonical_json_sha256(_without_digest(self, "descriptor_sha256"))
        if expected != self.descriptor_sha256:
            raise ValueError("observer descriptor digest does not match its canonical payload")
        return self

    @classmethod
    def seal(cls, payload: ObserverDescriptorPayload) -> ObserverDescriptor:
        document = payload.model_dump(mode="json", by_alias=True, exclude_none=True)
        return cls(**document, descriptor_sha256=canonical_json_sha256(document))

    def support_for(self, contract_id: str) -> ObserverContractSupport:
        for support in self.contracts:
            if support.contract_id == contract_id:
                return support
        raise ValueError(f"observer does not support contract: {contract_id}")


class ObservationRequestPayload(Stove0ProtocolModel):
    format: Literal["stove0-observation-request/v1"] = OBSERVATION_REQUEST_FORMAT
    work_id: Sha256
    observer_registration_id: RegistrationId
    observer_descriptor_sha256: Sha256
    observer_contract_id: SemanticId
    observer_contract_sha256: Sha256
    subjects: tuple[ArtifactSubject, ...] = Field(min_length=1)
    options: dict[str, JsonValue] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=300, ge=1, le=86400)
    maximum_result_bytes: int = Field(default=1024 * 1024, ge=1, le=64 * 1024 * 1024)
    retrieval_policy: RetrievalPolicy = "available-only"

    @field_validator("subjects")
    @classmethod
    def canonical_subjects(cls, value: tuple[ArtifactSubject, ...]) -> tuple[ArtifactSubject, ...]:
        ids = [item.id for item in value]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("observation subjects must be unique and ordered by artifact ID")
        return value


class ObservationRequest(ObservationRequestPayload):
    request_id: Sha256

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        if canonical_json_sha256(_without_digest(self, "request_id")) != self.request_id:
            raise ValueError("observation request id does not match its canonical payload")
        return self

    @classmethod
    def seal(cls, payload: ObservationRequestPayload) -> ObservationRequest:
        document = payload.model_dump(mode="json", by_alias=True, exclude_none=True)
        return cls(**document, request_id=canonical_json_sha256(document))


class ObserverRuntimeAuthority(Stove0ProtocolModel):
    """Secret-bearing invocation material excluded from durable request identity."""

    transport: Literal["riverhog-capability/v1"] = RIVERHOG_CAPABILITY_TRANSPORT
    riverhog_base_url: str = Field(min_length=1, max_length=2048)
    capability_token: str = Field(min_length=1, max_length=4096, repr=False)
    allow_insecure_http: bool = False
    workspace_assurance: Literal["encrypted", "ephemeral"]


class ObservationInvocation(Stove0ProtocolModel):
    """Fence-bound invocation authority excluded from semantic request identity."""

    request: ObservationRequest
    claim_id: str = Field(min_length=1, max_length=160)
    fence: int = Field(ge=1)
    runtime: ObserverRuntimeAuthority

    @field_validator("claim_id")
    @classmethod
    def canonical_claim_id(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("claim id must be canonical")
        return value


class ObserverImplementation(Stove0ProtocolModel):
    protocol: Literal["stove0-content-observer/v1"] = OBSERVER_PROTOCOL
    id: SemanticId
    version: str = Field(min_length=1, max_length=120)
    source_revision: str = Field(min_length=1, max_length=200)
    descriptor_sha256: Sha256


class ObservationFailure(Stove0ProtocolModel):
    code: SemanticId
    message: str = Field(min_length=1, max_length=1000)
    retryable: bool


class ObservationInapplicable(Stove0ProtocolModel):
    code: SemanticId
    message: str = Field(min_length=1, max_length=1000)


class ObservationResultPayload(Stove0ProtocolModel):
    format: Literal["stove0-observation-result/v1"] = OBSERVATION_RESULT_FORMAT
    request_id: Sha256
    state: ObservationState
    observer: ObserverImplementation
    observer_contract_id: SemanticId
    observer_contract_sha256: Sha256
    subjects: tuple[ArtifactSubject, ...] = Field(min_length=1)
    facts_schema: JsonSchemaDocument | None = None
    facts: dict[str, JsonValue] | None = None
    facts_sha256: Sha256 | None = None
    execution_evidence: dict[str, JsonValue] = Field(default_factory=dict)
    inapplicable: ObservationInapplicable | None = None
    failure: ObservationFailure | None = None

    @field_validator("subjects")
    @classmethod
    def canonical_subjects(cls, value: tuple[ArtifactSubject, ...]) -> tuple[ArtifactSubject, ...]:
        ids = [item.id for item in value]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("observation result subjects must be unique and ordered")
        return value

    @model_validator(mode="after")
    def validate_state_payload(self) -> Self:
        if self.state == "observed":
            if self.facts_schema is None or self.facts is None or self.facts_sha256 is None:
                raise ValueError("observed result requires facts and their schema")
            if canonical_json_sha256(self.facts) != self.facts_sha256:
                raise ValueError("observation facts digest does not match canonical facts")
            if self.inapplicable is not None or self.failure is not None:
                raise ValueError("observed result cannot include a terminal outcome")
        else:
            has_facts = (
                self.facts_schema is not None
                or self.facts is not None
                or self.facts_sha256 is not None
            )
            if has_facts:
                raise ValueError("non-observed result cannot include facts")
            if self.state == "inapplicable" and self.inapplicable is None:
                raise ValueError("inapplicable observation requires outcome details")
            if self.state != "inapplicable" and self.inapplicable is not None:
                raise ValueError("only inapplicable observation may include its outcome")
            if self.state == "failed" and self.failure is None:
                raise ValueError("failed observation requires failure details")
            if self.state != "failed" and self.failure is not None:
                raise ValueError("only failed observation may include failure details")
        return self


class ObservationResult(ObservationResultPayload):
    result_sha256: Sha256

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        if canonical_json_sha256(_without_digest(self, "result_sha256")) != self.result_sha256:
            raise ValueError("observation result digest does not match its canonical payload")
        return self

    @classmethod
    def seal(cls, payload: ObservationResultPayload) -> ObservationResult:
        document = payload.model_dump(mode="json", by_alias=True, exclude_none=True)
        return cls(**document, result_sha256=canonical_json_sha256(document))


class ObservationEvidence(Stove0ProtocolModel):
    """Complete routing evidence: immutable request plus accepted result."""

    request: ObservationRequest
    result: ObservationResult

    @model_validator(mode="after")
    def bind_result(self) -> Self:
        if self.result.state != "observed":
            raise ValueError("only observed results may become planning evidence")
        if (
            self.result.request_id != self.request.request_id
            or self.result.observer_contract_id != self.request.observer_contract_id
            or self.result.observer_contract_sha256 != self.request.observer_contract_sha256
            or self.result.observer.descriptor_sha256 != self.request.observer_descriptor_sha256
            or self.result.subjects != self.request.subjects
        ):
            raise ValueError("observation evidence result does not bind its request")
        return self


class WorkflowPlanPayload(Stove0ProtocolModel):
    format: Literal["stove0-workflow-plan/v1"] = WORKFLOW_PLAN_FORMAT
    work: WorkIdentity
    observations: tuple[ObservationEvidence, ...] = ()
    operation: OperationRef
    result_kind: OperationResultKind = "collection"
    target_registration_id: RegistrationId
    target_contract_sha256: Sha256
    requested_target_options: dict[str, JsonValue] = Field(default_factory=dict)
    input_retrieval_policy: RetrievalPolicy = "available-only"
    output_tags: tuple[str, ...] = ()
    retirement_policy: RetirementPolicy = "retain"
    retirement_grace_seconds: int = Field(default=0, ge=0)
    output_policy: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("observations")
    @classmethod
    def canonical_observations(
        cls, value: tuple[ObservationEvidence, ...]
    ) -> tuple[ObservationEvidence, ...]:
        ids = [item.request.request_id for item in value]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("workflow observations must be unique and ordered by request id")
        return value

    @field_validator("output_tags")
    @classmethod
    def canonical_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        tags = tuple(sorted(normalize_tag(item) for item in value))
        if tags != value or len(tags) != len(set(tags)):
            raise ValueError("workflow output tags must be unique and canonical")
        return tags

    @model_validator(mode="after")
    def protect_evaluation_sources(self) -> Self:
        if self.result_kind == "collection" and not self.output_tags:
            raise ValueError("collection-producing workflow requires output tags")
        if self.result_kind == "external-effect" and self.output_tags:
            raise ValueError("external-effect workflow cannot declare output tags")
        if self.result_kind == "external-effect" and self.retirement_policy != "retain":
            raise ValueError("external-effect workflow must retain source collections")
        if self.retirement_policy == "retain" and self.retirement_grace_seconds:
            raise ValueError("retained workflow plans cannot declare a retirement grace period")
        if self.work.evaluation is not None and self.retirement_policy != "retain":
            raise ValueError("evaluation and trial work must retain every source collection")
        return self


class WorkflowPlan(WorkflowPlanPayload):
    workflow_plan_sha256: Sha256

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        expected = canonical_json_sha256(_without_digest(self, "workflow_plan_sha256"))
        if expected != self.workflow_plan_sha256:
            raise ValueError("workflow plan digest does not match its canonical payload")
        return self

    @classmethod
    def seal(cls, payload: WorkflowPlanPayload) -> WorkflowPlan:
        document = payload.model_dump(mode="json", by_alias=True, exclude_none=True)
        return cls(**document, workflow_plan_sha256=canonical_json_sha256(document))


class WorkflowPlanIntent(Stove0ProtocolModel):
    """Work-independent fields that deterministically materialize a workflow plan."""

    operation: OperationRef
    result_kind: OperationResultKind = "collection"
    target_registration_id: RegistrationId
    target_contract_sha256: Sha256
    requested_target_options: dict[str, JsonValue] = Field(default_factory=dict)
    input_retrieval_policy: RetrievalPolicy = "available-only"
    output_tags: tuple[str, ...] = ()
    retirement_policy: RetirementPolicy = "retain"
    retirement_grace_seconds: int = Field(default=0, ge=0)
    output_policy: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("output_tags")
    @classmethod
    def canonical_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        tags = tuple(sorted(normalize_tag(item) for item in value))
        if tags != value or len(tags) != len(set(tags)):
            raise ValueError("workflow output tags must be unique and canonical")
        return tags

    @model_validator(mode="after")
    def validate_retirement(self) -> Self:
        if self.result_kind == "collection" and not self.output_tags:
            raise ValueError("collection-producing workflow intent requires output tags")
        if self.result_kind == "external-effect" and self.output_tags:
            raise ValueError("external-effect workflow intent cannot declare output tags")
        if self.result_kind == "external-effect" and self.retirement_policy != "retain":
            raise ValueError("external-effect workflow intent must retain source collections")
        if self.retirement_policy == "retain" and self.retirement_grace_seconds:
            raise ValueError("retained workflow intent cannot declare a retirement grace period")
        return self

    @classmethod
    def from_plan(cls, plan: WorkflowPlan) -> WorkflowPlanIntent:
        return cls(
            operation=plan.operation,
            result_kind=plan.result_kind,
            target_registration_id=plan.target_registration_id,
            target_contract_sha256=plan.target_contract_sha256,
            requested_target_options=plan.requested_target_options,
            input_retrieval_policy=plan.input_retrieval_policy,
            output_tags=plan.output_tags,
            retirement_policy=plan.retirement_policy,
            retirement_grace_seconds=plan.retirement_grace_seconds,
            output_policy=plan.output_policy,
        )

    def materialize(
        self,
        *,
        work: WorkIdentity,
        observations: tuple[ObservationEvidence, ...] = (),
    ) -> WorkflowPlan:
        return WorkflowPlan.seal(
            WorkflowPlanPayload(
                work=work,
                observations=observations,
                operation=self.operation,
                result_kind=self.result_kind,
                target_registration_id=self.target_registration_id,
                target_contract_sha256=self.target_contract_sha256,
                requested_target_options=self.requested_target_options,
                input_retrieval_policy=self.input_retrieval_policy,
                output_tags=self.output_tags,
                retirement_policy=self.retirement_policy,
                retirement_grace_seconds=self.retirement_grace_seconds,
                output_policy=self.output_policy,
            )
        )


class TargetPlanBinding(Stove0ProtocolModel):
    """Opaque binding to a target-owned preflight plan.

    The target protocol owns the plan schema and canonicalization algorithm. stove0
    retains the complete validated plan document and its target-issued digest, but
    deliberately does not reinterpret or re-hash the plan with stove0's canonical
    JSON rules. This prevents two authorities from disagreeing about target plan
    identity while preserving the full document in the execution envelope.
    """

    protocol: SemanticId
    target_implementation_id: SemanticId
    target_contract_sha256: Sha256
    operation_contract_sha256: Sha256
    plan: dict[str, JsonValue]
    plan_sha256: Sha256

    @field_validator("plan")
    @classmethod
    def require_plan_document(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        if not value:
            raise ValueError("target plan binding requires the complete plan document")
        return value


class ExecutionEnvelopePayload(Stove0ProtocolModel):
    format: Literal["stove0-execution-envelope/v1"] = EXECUTION_ENVELOPE_FORMAT
    claim_id: str = Field(min_length=1, max_length=160)
    fence: int = Field(ge=1)
    workflow_plan: WorkflowPlan
    target_plan: TargetPlanBinding

    @field_validator("claim_id")
    @classmethod
    def canonical_claim_id(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("execution claim id must be canonical")
        return value

    @model_validator(mode="after")
    def bind_target(self) -> Self:
        if self.workflow_plan.target_contract_sha256 != self.target_plan.target_contract_sha256:
            raise ValueError("target plan differs from the workflow-selected target contract")
        if self.workflow_plan.operation.sha256 != self.target_plan.operation_contract_sha256:
            raise ValueError("target plan differs from the workflow-selected operation contract")
        return self


class ExecutionEnvelope(ExecutionEnvelopePayload):
    execution_envelope_sha256: Sha256

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        if (
            canonical_json_sha256(_without_digest(self, "execution_envelope_sha256"))
            != self.execution_envelope_sha256
        ):
            raise ValueError("execution envelope digest does not match its canonical payload")
        return self

    @classmethod
    def seal(cls, payload: ExecutionEnvelopePayload) -> ExecutionEnvelope:
        document = payload.model_dump(mode="json", by_alias=True, exclude_none=True)
        return cls(**document, execution_envelope_sha256=canonical_json_sha256(document))


class EvaluationVariant(Stove0ProtocolModel):
    id: SemanticId
    parameters: dict[str, JsonValue] = Field(default_factory=dict)


class EvaluationMatrixPayload(Stove0ProtocolModel):
    format: Literal["stove0-evaluation-matrix/v1"] = EVALUATION_MATRIX_FORMAT
    variants: tuple[EvaluationVariant, ...] = Field(min_length=1)

    @field_validator("variants")
    @classmethod
    def canonical_variants(
        cls, value: tuple[EvaluationVariant, ...]
    ) -> tuple[EvaluationVariant, ...]:
        ids = [item.id for item in value]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("evaluation variants must be unique and ordered by ID")
        return value


class EvaluationMatrix(EvaluationMatrixPayload):
    matrix_sha256: Sha256

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        if canonical_json_sha256(_without_digest(self, "matrix_sha256")) != self.matrix_sha256:
            raise ValueError("evaluation matrix digest does not match its canonical payload")
        return self

    @classmethod
    def seal(cls, payload: EvaluationMatrixPayload) -> EvaluationMatrix:
        document = payload.model_dump(mode="json", by_alias=True, exclude_none=True)
        return cls(**document, matrix_sha256=canonical_json_sha256(document))


class EvaluationDefinitionPayload(Stove0ProtocolModel):
    format: Literal["stove0-evaluation-definition/v1"] = EVALUATION_DEFINITION_FORMAT
    purpose: Literal["trial", "evaluation"] = "evaluation"
    recipe: RecipeRef
    inputs: tuple[CollectionRootRef, ...] = Field(min_length=1)
    common_intent: dict[str, JsonValue] = Field(default_factory=dict)
    matrix: EvaluationMatrix

    @field_validator("inputs")
    @classmethod
    def canonical_inputs(
        cls, value: tuple[CollectionRootRef, ...]
    ) -> tuple[CollectionRootRef, ...]:
        ordered = tuple(
            sorted(value, key=lambda item: (item.collection_id, item.archive_root_sha256))
        )
        if value != ordered or len(value) != len({item.collection_id for item in value}):
            raise ValueError("evaluation inputs must be unique and canonically ordered")
        return value

    @model_validator(mode="after")
    def validate_purpose(self) -> Self:
        if self.purpose == "trial" and len(self.matrix.variants) != 1:
            raise ValueError("materialized trial definitions require exactly one variant")
        return self


class EvaluationDefinition(EvaluationDefinitionPayload):
    evaluation_id: Sha256

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        if canonical_json_sha256(_without_digest(self, "evaluation_id")) != self.evaluation_id:
            raise ValueError("evaluation id does not match its canonical definition")
        return self

    @classmethod
    def seal(cls, payload: EvaluationDefinitionPayload) -> EvaluationDefinition:
        document = payload.model_dump(mode="json", by_alias=True, exclude_none=True)
        return cls(**document, evaluation_id=canonical_json_sha256(document))

    def child_work(self, variant_id: str) -> WorkIdentity:
        variant = next((item for item in self.matrix.variants if item.id == variant_id), None)
        if variant is None:
            raise ValueError(f"unknown evaluation variant: {variant_id}")
        return WorkIdentity.seal(
            WorkPayload(
                recipe=self.recipe,
                inputs=self.inputs,
                effective_intent=self.common_intent,
                evaluation=EvaluationBinding(
                    evaluation_id=self.evaluation_id,
                    matrix_sha256=self.matrix.matrix_sha256,
                    variant_id=variant.id,
                    parameters=variant.parameters,
                ),
            )
        )

    def child_works(self) -> tuple[WorkIdentity, ...]:
        return tuple(self.child_work(item.id) for item in self.matrix.variants)


class WorkflowPreviewRequestPayload(Stove0ProtocolModel):
    format: Literal["stove0-workflow-preview-request/v1"] = WORKFLOW_PREVIEW_REQUEST_FORMAT
    work: WorkIdentity


class WorkflowPreviewRequest(WorkflowPreviewRequestPayload):
    preview_id: Sha256

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        if canonical_json_sha256(_without_digest(self, "preview_id")) != self.preview_id:
            raise ValueError("workflow preview id does not match its canonical request")
        return self

    @classmethod
    def seal(cls, payload: WorkflowPreviewRequestPayload) -> WorkflowPreviewRequest:
        document = payload.model_dump(mode="json", by_alias=True, exclude_none=True)
        return cls(**document, preview_id=canonical_json_sha256(document))


class PreviewOutcome(Stove0ProtocolModel):
    code: SemanticId
    message: str = Field(min_length=1, max_length=1000)
    retryable: bool | None = None


class ControllerEvidencePayload(Stove0ProtocolModel):
    format: Literal["stove0-controller-evidence/v1"] = CONTROLLER_EVIDENCE_FORMAT
    execution_envelope: ExecutionEnvelope


class ControllerEvidence(ControllerEvidencePayload):
    controller_evidence_sha256: Sha256

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        if (
            canonical_json_sha256(_without_digest(self, "controller_evidence_sha256"))
            != self.controller_evidence_sha256
        ):
            raise ValueError("controller evidence digest does not match its canonical payload")
        return self

    @classmethod
    def seal(cls, payload: ControllerEvidencePayload) -> ControllerEvidence:
        document = payload.model_dump(mode="json", by_alias=True, exclude_none=True)
        return cls(**document, controller_evidence_sha256=canonical_json_sha256(document))


__all__ = [
    "ARTIFACT_ID_PATTERN",
    "CONTROLLER_EVIDENCE_FORMAT",
    "ControllerEvidence",
    "ControllerEvidencePayload",
    "EVALUATION_DEFINITION_FORMAT",
    "EVALUATION_MATRIX_FORMAT",
    "EvaluationBinding",
    "EvaluationDefinition",
    "EvaluationDefinitionPayload",
    "EvaluationMatrix",
    "EvaluationMatrixPayload",
    "EvaluationVariant",
    "EXECUTION_ENVELOPE_FORMAT",
    "ExecutionEnvelope",
    "ExecutionEnvelopePayload",
    "ForkJoinBinding",
    "JoinWorkBinding",
    "JoinWorkMemberBinding",
    "JsonSchemaDocument",
    "JSON_SCHEMA_ONLY_SEMANTIC_PROFILE",
    "OBSERVER_PROTOCOL",
    "OBSERVATION_REQUEST_FORMAT",
    "OBSERVATION_RESULT_FORMAT",
    "ObservationEvidence",
    "ObservationFailure",
    "ObservationInapplicable",
    "ObservationInvocation",
    "ObservationRequest",
    "ObservationRequestPayload",
    "ObservationResult",
    "ObservationResultPayload",
    "ObservationState",
    "OperationResultKind",
    "ObserverContract",
    "ObserverContractPayload",
    "ObserverContractSupport",
    "ObserverDescriptor",
    "ObserverDescriptorPayload",
    "ObserverImplementation",
    "ObserverRuntimeAuthority",
    "OperationRef",
    "PreviewOutcome",
    "RIVERHOG_CAPABILITY_TRANSPORT",
    "RecipeRef",
    "RetirementPolicy",
    "SHA256_PATTERN",
    "SemanticId",
    "SemanticValidationProfile",
    "SemanticValidationProfilePayload",
    "Sha256",
    "Stove0ProtocolModel",
    "TargetPlanBinding",
    "WORKFLOW_PLAN_FORMAT",
    "WORKFLOW_PREVIEW_FORMAT",
    "WORKFLOW_PREVIEW_REQUEST_FORMAT",
    "WORK_FORMAT",
    "WorkIdentity",
    "WorkPayload",
    "WorkflowPlan",
    "WorkflowPlanIntent",
    "WorkflowPlanPayload",
    "WorkflowPreviewRequest",
    "WorkflowPreviewRequestPayload",
    "ArtifactSubject",
    "BranchWorkBinding",
    "CollectionRootRef",
    "canonical_json_bytes",
    "canonical_json_sha256",
]

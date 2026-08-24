"""Typed HTTP documents for Riverhog-owned collection work.

Application work and controller evidence remain opaque canonical JSON.  The
surrounding identities, custody roots, capabilities, claims, settlement, and
derivation documents are Riverhog contracts and are represented exactly here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from riverhog_protocol.collection_workflows import (
    ArtifactDisposition,
    CollectionArtifactIdentity,
    CollectionDerivation,
    CollectionProcessingOutcomeIdentity,
    CollectionRootIdentity,
    OperationIdentity,
    RecipeIdentity,
    RetirementPolicy,
    canonical_json_bytes,
    canonical_json_sha256,
)
from riverhog_protocol.paths import normalize_tag

SHA256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
SemanticId = Annotated[
    str,
    Field(pattern=r"^[a-z0-9](?:[a-z0-9._/-]{0,158}[a-z0-9])?$"),
]
Timestamp = Annotated[str, Field(min_length=1, max_length=64)]
ClaimState = Literal["active", "settled", "retiring", "abandoned", "released"]
CapabilityAction = Literal["read-inputs", "write-output"]

WORK_DOCUMENT_MAX_BYTES = 4 * 1024 * 1024
CONTROLLER_EVIDENCE_MAX_BYTES = 16 * 1024 * 1024


class RiverhogWorkflowDocument(BaseModel):
    """Closed JSON document with deliberate key lookup convenience."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    def __getitem__(self, key: str) -> Any:
        return self.model_dump(mode="json", exclude_none=False)[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.model_dump(mode="json", exclude_none=False).get(key, default)


def _validate_opaque_document(
    value: Mapping[str, Any],
    digest: str,
    *,
    label: str,
    maximum_bytes: int,
) -> None:
    encoded = canonical_json_bytes(value)
    if len(encoded) > maximum_bytes:
        raise ValueError(f"{label} exceeds {maximum_bytes} canonical JSON bytes")
    if canonical_json_sha256(value) != digest:
        raise ValueError(f"{label} identity does not match its canonical JSON")


class CollectionRootIdentityDocument(RiverhogWorkflowDocument):
    collection_id: int = Field(ge=1)
    archive_root_sha256: SHA256
    content_identity: SHA256

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        CollectionRootIdentity.from_mapping(self.model_dump(mode="json"))
        return self


class CollectionArtifactIdentityDocument(RiverhogWorkflowDocument):
    collection: CollectionRootIdentityDocument
    path: str = Field(min_length=1, max_length=4096)
    bytes: int = Field(ge=0)
    sha256: SHA256

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        CollectionArtifactIdentity.from_mapping(self.model_dump(mode="json"))
        return self


class OperationIdentityDocument(RiverhogWorkflowDocument):
    id: SemanticId
    sha256: SHA256

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        OperationIdentity.from_mapping(self.model_dump(mode="json"))
        return self


class RecipeIdentityDocument(RiverhogWorkflowDocument):
    id: SemanticId
    revision: int = Field(ge=1)
    sha256: SHA256

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        RecipeIdentity.from_mapping(self.model_dump(mode="json"))
        return self


class ProcessingOutcomeIdentityDocument(RiverhogWorkflowDocument):
    outcome_id: SemanticId
    source_claim_id: SHA256
    output_collection: CollectionRootIdentityDocument
    derivation_sha256: SHA256

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        CollectionProcessingOutcomeIdentity.from_mapping(self.model_dump(mode="json"))
        return self


class ArtifactDispositionInputDocument(RiverhogWorkflowDocument):
    collection_id: int = Field(ge=1)
    archive_root_sha256: SHA256
    path: str = Field(min_length=1, max_length=4096)


class ArtifactDispositionFailureDocument(RiverhogWorkflowDocument):
    code: SemanticId
    message: str = Field(min_length=1, max_length=500)


class ArtifactDispositionDocument(RiverhogWorkflowDocument):
    input: ArtifactDispositionInputDocument
    status: Literal["transformed", "preserved", "omitted", "rejected"]
    outputs: list[str] = Field(default_factory=list)
    failure: ArtifactDispositionFailureDocument | None = None

    @model_validator(mode="after")
    def validate_disposition(self) -> Self:
        ArtifactDisposition.from_mapping(self.model_dump(mode="json", exclude_none=True))
        return self


class ClaimFenceDocument(RiverhogWorkflowDocument):
    id: str = Field(min_length=1, max_length=160)
    fence: int = Field(ge=1)


class CollectionDerivationDocument(RiverhogWorkflowDocument):
    format: Literal["riverhog-collection-derivation/v1"]
    execution_id: SHA256
    claim: ClaimFenceDocument
    recipe: RecipeIdentityDocument
    operation: OperationIdentityDocument
    inputs: list[CollectionRootIdentityDocument] = Field(min_length=1)
    output_tags: list[str] = Field(min_length=1)
    execution_envelope_sha256: SHA256
    execution_sha256: SHA256
    controller_evidence: dict[str, Any]
    controller_evidence_sha256: SHA256
    dispositions: list[ArtifactDispositionDocument] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_derivation(self) -> Self:
        CollectionDerivation.from_mapping(self.model_dump(mode="json"))
        _validate_opaque_document(
            self.controller_evidence,
            self.controller_evidence_sha256,
            label="controller evidence",
            maximum_bytes=CONTROLLER_EVIDENCE_MAX_BYTES,
        )
        return self


class ProcessingClaimCreateDocument(RiverhogWorkflowDocument):
    work_id: SHA256
    work_document: dict[str, Any]
    work_document_sha256: SHA256
    inputs: list[CollectionRootIdentityDocument] = Field(min_length=1)
    lease_seconds: int = Field(default=1800, ge=30, le=86400)
    purpose: str = Field(default="collection-work/v1", min_length=1, max_length=160)

    @model_validator(mode="after")
    def validate_claim(self) -> Self:
        roots = tuple(
            CollectionRootIdentity.from_mapping(item.model_dump(mode="json"))
            for item in self.inputs
        )
        if roots != tuple(sorted(roots)) or len({item.collection_id for item in roots}) != len(
            roots
        ):
            raise ValueError("input collection roots must be unique and canonically ordered")
        _validate_opaque_document(
            self.work_document,
            self.work_document_sha256,
            label="work document",
            maximum_bytes=WORK_DOCUMENT_MAX_BYTES,
        )
        return self


class ProcessingClaimRenewDocument(RiverhogWorkflowDocument):
    fence: int = Field(ge=1)
    lease_seconds: int = Field(default=1800, ge=30, le=86400)


class ProcessingClaimRestartDocument(ProcessingClaimRenewDocument):
    pass


class ProcessingClaimPlanSealDocument(RiverhogWorkflowDocument):
    fence: int = Field(ge=1)
    execution_id: SHA256
    controller_evidence: dict[str, Any]
    controller_evidence_sha256: SHA256
    operation: OperationIdentityDocument
    input_artifacts: list[CollectionArtifactIdentityDocument] = Field(min_length=1)
    output_tags: list[str] = Field(min_length=1)
    retirement_policy: RetirementPolicy = "retain"
    retirement_grace_seconds: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        _validate_opaque_document(
            self.controller_evidence,
            self.controller_evidence_sha256,
            label="controller evidence",
            maximum_bytes=CONTROLLER_EVIDENCE_MAX_BYTES,
        )
        artifacts = tuple(
            CollectionArtifactIdentity.from_mapping(item.model_dump(mode="json"))
            for item in self.input_artifacts
        )
        keys = [(item.collection.collection_id, item.path) for item in artifacts]
        if len(keys) != len(set(keys)):
            raise ValueError("exact artifact scope must not repeat a collection file")
        normalized_tags = tuple(sorted(normalize_tag(item) for item in self.output_tags))
        if tuple(self.output_tags) != normalized_tags or len(normalized_tags) != len(
            set(normalized_tags)
        ):
            raise ValueError("output tags must be nonempty, unique, and canonical")
        if self.retirement_policy == "retain" and self.retirement_grace_seconds:
            raise ValueError("retained collection work cannot declare retirement grace")
        return self


def _default_capability_actions() -> list[CapabilityAction]:
    return ["read-inputs"]


class TransformCapabilityCreateDocument(RiverhogWorkflowDocument):
    fence: int = Field(ge=1)
    audience: str = Field(pattern=r"^[a-z0-9][a-z0-9._:/-]{0,299}$")
    actions: list[CapabilityAction] = Field(
        default_factory=_default_capability_actions,
        min_length=1,
    )
    artifacts: list[CollectionArtifactIdentityDocument] = Field(min_length=1)
    ttl_seconds: int = Field(default=900, ge=30, le=86400)

    @model_validator(mode="after")
    def validate_capability(self) -> Self:
        if self.actions != sorted(set(self.actions)):
            raise ValueError("capability actions must be unique and canonically ordered")
        artifacts = [
            CollectionArtifactIdentity.from_mapping(item.model_dump(mode="json"))
            for item in self.artifacts
        ]
        keys = [(item.collection.collection_id, item.path) for item in artifacts]
        if len(keys) != len(set(keys)):
            raise ValueError("capability artifact scope must be exact and unique")
        return self


class ProcessingOutcomeBindingDocument(RiverhogWorkflowDocument):
    claim_id: SHA256
    fence: int = Field(ge=1)
    outcome_id: SemanticId


class ProcessingClaimSettleDocument(RiverhogWorkflowDocument):
    fence: int = Field(ge=1)
    output_collection_id: int = Field(ge=1)
    derivation: CollectionDerivationDocument
    outcome: ProcessingOutcomeBindingDocument | None = None


class ProcessingClaimOutcomesSettleDocument(RiverhogWorkflowDocument):
    fence: int = Field(ge=1)
    outcomes: list[ProcessingOutcomeIdentityDocument] = Field(min_length=1)
    retirement_policy: RetirementPolicy = "retain"
    retirement_grace_seconds: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_outcomes(self) -> Self:
        outcomes = tuple(
            CollectionProcessingOutcomeIdentity.from_mapping(item.model_dump(mode="json"))
            for item in self.outcomes
        )
        if outcomes != tuple(sorted(outcomes)):
            raise ValueError("processing outcomes must be canonically ordered")
        ids = [item.outcome_id for item in outcomes]
        claims = [item.source_claim_id for item in outcomes]
        collections = [item.output_collection.collection_id for item in outcomes]
        if any(len(values) != len(set(values)) for values in (ids, claims, collections)):
            raise ValueError("processing outcomes must be exact and unique")
        if self.retirement_policy == "retain" and self.retirement_grace_seconds:
            raise ValueError("retained collection work cannot declare retirement grace")
        return self


class ProcessingClaimFenceDocument(RiverhogWorkflowDocument):
    fence: int = Field(ge=1)


class ProcessingClaimAbandonDocument(ProcessingClaimFenceDocument):
    reason: str = Field(min_length=1, max_length=1000)


class ProcessingClaimConsumerDocument(RiverhogWorkflowDocument):
    app: SemanticId
    key_id: str | None = Field(default=None, min_length=1, max_length=300)


class ProcessingClaimPlanDocument(RiverhogWorkflowDocument):
    execution_id: SHA256
    controller_evidence: dict[str, Any]
    controller_evidence_sha256: SHA256
    operation: OperationIdentityDocument
    input_artifacts: list[CollectionArtifactIdentityDocument] = Field(min_length=1)
    output_tags: list[str] = Field(min_length=1)
    retirement_policy: RetirementPolicy
    retirement_grace_seconds: int = Field(ge=0)
    sealed_at: Timestamp

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        ProcessingClaimPlanSealDocument(
            fence=1,
            execution_id=self.execution_id,
            controller_evidence=self.controller_evidence,
            controller_evidence_sha256=self.controller_evidence_sha256,
            operation=self.operation,
            input_artifacts=self.input_artifacts,
            output_tags=self.output_tags,
            retirement_policy=self.retirement_policy,
            retirement_grace_seconds=self.retirement_grace_seconds,
        )
        return self


class ProcessingClaimOutcomeSettlementDocument(RiverhogWorkflowDocument):
    outcomes_sha256: SHA256
    retirement_policy: RetirementPolicy
    retirement_grace_seconds: int = Field(ge=0)


class ProcessingClaimDocument(RiverhogWorkflowDocument):
    format: Literal["riverhog-processing-claim/v1"]
    id: SHA256
    work_id: SHA256
    consumer: ProcessingClaimConsumerDocument
    purpose: str = Field(min_length=1, max_length=160)
    state: ClaimState
    fence: int = Field(ge=1)
    expires_at: Timestamp
    created_at: Timestamp
    updated_at: Timestamp
    settled_at: Timestamp | None = None
    abandoned_at: Timestamp | None = None
    abandonment_reason: str | None = Field(default=None, min_length=1, max_length=1000)
    released_at: Timestamp | None = None
    output_collection_id: int | None = Field(default=None, ge=1)
    work_document: dict[str, Any]
    work_document_sha256: SHA256
    inputs: list[CollectionRootIdentityDocument] = Field(min_length=1)
    plan: ProcessingClaimPlanDocument | None = None
    outcomes: list[ProcessingOutcomeIdentityDocument] = Field(default_factory=list)
    outcome_settlement: ProcessingClaimOutcomeSettlementDocument | None = None

    @model_validator(mode="after")
    def validate_claim(self) -> Self:
        ProcessingClaimCreateDocument(
            work_id=self.work_id,
            work_document=self.work_document,
            work_document_sha256=self.work_document_sha256,
            inputs=self.inputs,
            purpose=self.purpose,
        )
        outcomes = [item.model_dump(mode="json") for item in self.outcomes]
        if outcomes:
            ProcessingClaimOutcomesSettleDocument(fence=self.fence, outcomes=self.outcomes)
        if self.outcome_settlement is not None:
            if canonical_json_sha256(outcomes) != self.outcome_settlement.outcomes_sha256:
                raise ValueError("processing outcome settlement identity is invalid")
            if self.plan is not None or self.state not in {"settled", "retiring", "released"}:
                raise ValueError("processing outcome settlement is inconsistent with claim state")
        if self.state == "abandoned" and (
            self.abandoned_at is None or self.abandonment_reason is None
        ):
            raise ValueError("abandoned claim has no abandonment evidence")
        if self.state == "released" and self.released_at is None:
            raise ValueError("released claim has no release evidence")
        return self


class ProcessingClaimFiltersDocument(RiverhogWorkflowDocument):
    state: ClaimState | None = None


class ProcessingClaimPageDocument(RiverhogWorkflowDocument):
    page: int = Field(ge=1)
    per_page: int = Field(ge=0)
    total: int = Field(ge=0)
    pages: int = Field(ge=0)
    sort: Literal["created_at", "updated_at", "expires_at", "state", "work_id", "execution_id"]
    order: Literal["asc", "desc"]
    filters: ProcessingClaimFiltersDocument
    claims: list[ProcessingClaimDocument]


class TransformCapabilityDocument(RiverhogWorkflowDocument):
    format: Literal["riverhog-transform-capability/v1"]
    id: str = Field(min_length=1, max_length=160)
    claim_id: SHA256
    fence: int = Field(ge=1)
    audience: str = Field(pattern=r"^[a-z0-9][a-z0-9._:/-]{0,299}$")
    actions: list[CapabilityAction] = Field(min_length=1)
    principal_app: str = Field(min_length=1, max_length=300)
    expires_at: Timestamp
    artifacts: list[CollectionArtifactIdentityDocument] = Field(min_length=1)
    token: str = Field(pattern=r"^rhc_[A-Za-z0-9_-]+$")

    @model_validator(mode="after")
    def validate_capability(self) -> Self:
        TransformCapabilityCreateDocument(
            fence=self.fence,
            audience=self.audience,
            actions=self.actions,
            artifacts=self.artifacts,
        )
        return self


class CollectionDerivationResponseDocument(RiverhogWorkflowDocument):
    collection_id: int = Field(ge=1)
    document_sha256: SHA256
    derivation: CollectionDerivationDocument

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        document = CollectionDerivation.from_mapping(self.derivation.model_dump(mode="json"))
        if document.sha256 != self.document_sha256:
            raise ValueError("collection derivation identity does not match its document")
        return self


__all__ = [
    "CONTROLLER_EVIDENCE_MAX_BYTES",
    "CapabilityAction",
    "ClaimState",
    "CollectionArtifactIdentityDocument",
    "CollectionDerivationDocument",
    "CollectionDerivationResponseDocument",
    "CollectionRootIdentityDocument",
    "OperationIdentityDocument",
    "ProcessingClaimAbandonDocument",
    "ProcessingClaimCreateDocument",
    "ProcessingClaimDocument",
    "ProcessingClaimFenceDocument",
    "ProcessingClaimOutcomesSettleDocument",
    "ProcessingClaimPageDocument",
    "ProcessingClaimPlanSealDocument",
    "ProcessingClaimRenewDocument",
    "ProcessingClaimRestartDocument",
    "ProcessingClaimSettleDocument",
    "ProcessingOutcomeBindingDocument",
    "ProcessingOutcomeIdentityDocument",
    "RecipeIdentityDocument",
    "RiverhogWorkflowDocument",
    "TransformCapabilityCreateDocument",
    "TransformCapabilityDocument",
    "WORK_DOCUMENT_MAX_BYTES",
]

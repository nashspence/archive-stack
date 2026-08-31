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
    ArtifactDispositionOutput,
    ArtifactDispositionSetIdentity,
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
from riverhog_protocol.list_controls import ClaimState, ProcessingClaimSort, SortOrder
from riverhog_protocol.paths import CanonicalRelPath, CanonicalTag, CollectionId

SHA256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ProcessingClaimId = SHA256
SemanticId = Annotated[
    str,
    Field(pattern=r"^[a-z0-9](?:[a-z0-9._/-]{0,158}[a-z0-9])?$"),
]
Timestamp = Annotated[str, Field(min_length=1, max_length=64)]
CapabilityAction = Literal["read-inputs", "write-output"]

WORK_DOCUMENT_MAX_BYTES = 4 * 1024 * 1024
CONTROLLER_EVIDENCE_MAX_BYTES = 16 * 1024 * 1024
DISPOSITION_BATCH_MAX = 128
WORKFLOW_SET_BATCH_MAX = 128


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
    collection_id: CollectionId
    archive_root_sha256: SHA256
    content_identity: SHA256

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        CollectionRootIdentity.from_mapping(self.model_dump(mode="json"))
        return self


class CollectionArtifactIdentityDocument(RiverhogWorkflowDocument):
    collection: CollectionRootIdentityDocument
    path: CanonicalRelPath
    bytes: int = Field(ge=0)
    sha256: SHA256

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        CollectionArtifactIdentity.from_mapping(self.model_dump(mode="json"))
        return self


class ExactSetAuthorityDocument(RiverhogWorkflowDocument):
    """Small immutable identity for an exact canonically ordered logical set."""

    count: int = Field(ge=1)
    sha256: SHA256


class ArtifactSetAuthorityDocument(ExactSetAuthorityDocument):
    total_bytes: int = Field(ge=0)


class ReceivingSetDocument(RiverhogWorkflowDocument):
    state: Literal["receiving", "sealed"]
    count: int = Field(ge=0)
    authority: ExactSetAuthorityDocument | None = None

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if (self.state == "sealed") != (self.authority is not None):
            raise ValueError("set authority is inconsistent with state")
        if self.authority is not None and self.authority.count != self.count:
            raise ValueError("set authority count differs from staged count")
        return self


class OutcomeSetDocument(RiverhogWorkflowDocument):
    state: Literal["receiving", "sealing", "sealed", "failed"]
    count: int = Field(ge=0)
    authority: ExactSetAuthorityDocument | None = None
    failure: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if (self.state == "sealed") != (self.authority is not None):
            raise ValueError("outcome authority is inconsistent with state")
        if (self.state == "failed") != (self.failure is not None):
            raise ValueError("outcome failure is inconsistent with state")
        if self.authority is not None and self.authority.count != self.count:
            raise ValueError("outcome authority count differs from staged count")
        return self


class ArtifactReceivingSetDocument(RiverhogWorkflowDocument):
    state: Literal["receiving", "sealed"]
    count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    authority: ArtifactSetAuthorityDocument | None = None

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if (self.state == "sealed") != (self.authority is not None):
            raise ValueError("artifact authority is inconsistent with state")
        if self.authority is not None and (
            self.authority.count != self.count or self.authority.total_bytes != self.total_bytes
        ):
            raise ValueError("artifact authority totals differ from staged totals")
        return self


class CollectionRootBatchDocument(RiverhogWorkflowDocument):
    fence: int = Field(ge=1)
    start_ordinal: int = Field(ge=0)
    inputs: list[CollectionRootIdentityDocument] = Field(
        min_length=1,
        max_length=WORKFLOW_SET_BATCH_MAX,
        json_schema_extra={"uniqueItems": True},
    )


class CollectionRootPageDocument(RiverhogWorkflowDocument):
    authority: ExactSetAuthorityDocument
    start_ordinal: int = Field(ge=0)
    next_ordinal: int | None = Field(default=None, ge=1)
    inputs: list[CollectionRootIdentityDocument] = Field(max_length=WORKFLOW_SET_BATCH_MAX)


class CollectionArtifactBatchDocument(RiverhogWorkflowDocument):
    fence: int = Field(ge=1)
    start_ordinal: int = Field(ge=0)
    artifacts: list[CollectionArtifactIdentityDocument] = Field(
        min_length=1,
        max_length=WORKFLOW_SET_BATCH_MAX,
        json_schema_extra={"uniqueItems": True},
    )


class CollectionArtifactPageDocument(RiverhogWorkflowDocument):
    authority: ArtifactSetAuthorityDocument
    start_ordinal: int = Field(ge=0)
    next_ordinal: int | None = Field(default=None, ge=1)
    artifacts: list[CollectionArtifactIdentityDocument] = Field(max_length=WORKFLOW_SET_BATCH_MAX)


class OutputTagBatchDocument(RiverhogWorkflowDocument):
    fence: int = Field(ge=1)
    start_ordinal: int = Field(ge=0)
    tags: list[CanonicalTag] = Field(
        min_length=1,
        max_length=WORKFLOW_SET_BATCH_MAX,
        json_schema_extra={"uniqueItems": True},
    )


class OutputTagPageDocument(RiverhogWorkflowDocument):
    authority: ExactSetAuthorityDocument
    start_ordinal: int = Field(ge=0)
    next_ordinal: int | None = Field(default=None, ge=1)
    tags: list[CanonicalTag] = Field(max_length=WORKFLOW_SET_BATCH_MAX)


class ProcessingOutcomePageDocument(RiverhogWorkflowDocument):
    authority: ExactSetAuthorityDocument
    start_ordinal: int = Field(ge=0)
    next_ordinal: int | None = Field(default=None, ge=1)
    outcomes: list[ProcessingOutcomeIdentityDocument] = Field(max_length=WORKFLOW_SET_BATCH_MAX)


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
    source_claim_id: ProcessingClaimId
    output_collection: CollectionRootIdentityDocument
    derivation_sha256: SHA256

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        CollectionProcessingOutcomeIdentity.from_mapping(self.model_dump(mode="json"))
        return self


class ArtifactDispositionInputDocument(RiverhogWorkflowDocument):
    collection_id: CollectionId
    archive_root_sha256: SHA256
    path: CanonicalRelPath


class ArtifactDispositionFailureDocument(RiverhogWorkflowDocument):
    code: SemanticId
    message: str = Field(min_length=1, max_length=500)


class ArtifactDispositionDocument(RiverhogWorkflowDocument):
    model_config = ConfigDict(
        json_schema_extra={
            "oneOf": [
                {
                    "properties": {
                        "status": {"enum": ["transformed", "preserved"]},
                        "failure": {"type": "null"},
                    }
                },
                {
                    "properties": {
                        "status": {"enum": ["omitted", "rejected"]},
                        "failure": {"type": "object"},
                    },
                    "required": ["failure"],
                },
            ]
        }
    )

    input: ArtifactDispositionInputDocument
    status: Literal["transformed", "preserved", "omitted", "rejected"]
    failure: ArtifactDispositionFailureDocument | None = None

    @model_validator(mode="after")
    def validate_disposition(self) -> Self:
        ArtifactDisposition.from_mapping(self.model_dump(mode="json", exclude_none=True))
        return self


class ArtifactDispositionOutputDocument(RiverhogWorkflowDocument):
    input: ArtifactDispositionInputDocument
    output_path: CanonicalRelPath

    @model_validator(mode="after")
    def validate_output(self) -> Self:
        ArtifactDispositionOutput.from_mapping(self.model_dump(mode="json"))
        return self


class ArtifactDispositionBatchDocument(RiverhogWorkflowDocument):
    fence: int = Field(ge=1)
    dispositions: list[ArtifactDispositionDocument] = Field(
        min_length=1,
        max_length=DISPOSITION_BATCH_MAX,
        json_schema_extra={"uniqueItems": True},
    )


class ArtifactDispositionOutputBatchDocument(RiverhogWorkflowDocument):
    fence: int = Field(ge=1)
    outputs: list[ArtifactDispositionOutputDocument] = Field(
        min_length=1,
        max_length=DISPOSITION_BATCH_MAX,
        json_schema_extra={"uniqueItems": True},
    )


class ArtifactDispositionSetIdentityDocument(RiverhogWorkflowDocument):
    disposition_count: int = Field(ge=1)
    output_edge_count: int = Field(ge=1)
    output_artifact_count: int = Field(ge=1)
    sha256: SHA256

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        ArtifactDispositionSetIdentity.from_mapping(self.model_dump(mode="json"))
        return self


class ArtifactDispositionSetDocument(RiverhogWorkflowDocument):
    claim_id: ProcessingClaimId
    state: Literal["receiving", "sealing", "sealed", "failed"]
    disposition_count: int = Field(ge=0)
    output_edge_count: int = Field(ge=0)
    output_artifact_count: int = Field(ge=0)
    identity: ArtifactDispositionSetIdentityDocument | None = None
    failure: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if (self.state == "sealed") != (self.identity is not None):
            raise ValueError("sealed disposition set identity is inconsistent with state")
        if (self.state == "failed") != (self.failure is not None):
            raise ValueError("disposition set failure is inconsistent with state")
        if self.identity is not None and (
            self.disposition_count != self.identity.disposition_count
            or self.output_edge_count != self.identity.output_edge_count
            or self.output_artifact_count != self.identity.output_artifact_count
        ):
            raise ValueError("disposition set counts differ from its sealed identity")
        return self


class ArtifactDispositionPageDocument(RiverhogWorkflowDocument):
    authority: ArtifactDispositionSetIdentityDocument
    page: int = Field(ge=1)
    per_page: int = Field(ge=1, le=100)
    total: int = Field(ge=1)
    pages: int = Field(ge=1)
    dispositions: list[ArtifactDispositionDocument] = Field(max_length=100)


class ArtifactDispositionOutputPageDocument(RiverhogWorkflowDocument):
    authority: ArtifactDispositionSetIdentityDocument
    page: int = Field(ge=1)
    per_page: int = Field(ge=1, le=100)
    total: int = Field(ge=1)
    pages: int = Field(ge=1)
    outputs: list[ArtifactDispositionOutputDocument] = Field(max_length=100)


class ClaimFenceDocument(RiverhogWorkflowDocument):
    id: ProcessingClaimId
    fence: int = Field(ge=1)


class CollectionDerivationDocument(RiverhogWorkflowDocument):
    format: Literal["riverhog-collection-derivation/v1"]
    execution_id: SHA256
    claim: ClaimFenceDocument
    recipe: RecipeIdentityDocument
    operation: OperationIdentityDocument
    input_set_sha256: SHA256
    artifact_set_sha256: SHA256
    output_tag_set_sha256: SHA256
    execution_envelope_sha256: SHA256
    execution_sha256: SHA256
    controller_evidence: dict[str, Any]
    controller_evidence_sha256: SHA256
    disposition_set: ArtifactDispositionSetIdentityDocument

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
    lease_seconds: int = Field(default=1800, ge=30, le=86400)
    purpose: str = Field(default="collection-work/v1", min_length=1, max_length=160)

    @model_validator(mode="after")
    def validate_claim(self) -> Self:
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
    model_config = ConfigDict(
        json_schema_extra={
            "if": {"properties": {"retirement_policy": {"const": "retain"}}},
            "then": {"properties": {"retirement_grace_seconds": {"const": 0}}},
        }
    )

    fence: int = Field(ge=1)
    execution_id: SHA256
    controller_evidence: dict[str, Any]
    controller_evidence_sha256: SHA256
    operation: OperationIdentityDocument
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
        json_schema_extra={
            "oneOf": [
                {"const": ["read-inputs"]},
                {"const": ["read-inputs", "write-output"]},
            ]
        },
    )
    ttl_seconds: int = Field(default=900, ge=30, le=86400)

    @model_validator(mode="after")
    def validate_capability(self) -> Self:
        if self.actions not in (["read-inputs"], ["read-inputs", "write-output"]):
            raise ValueError(
                "capability actions must be read-inputs, optionally followed by write-output"
            )
        if self.actions != sorted(set(self.actions)):
            raise ValueError("capability actions must be unique and canonically ordered")
        return self


class ProcessingOutcomeBindingDocument(RiverhogWorkflowDocument):
    claim_id: ProcessingClaimId
    fence: int = Field(ge=1)
    outcome_id: SemanticId


class ProcessingClaimSettleDocument(RiverhogWorkflowDocument):
    fence: int = Field(ge=1)
    output_collection_id: CollectionId
    derivation: CollectionDerivationDocument
    outcome: ProcessingOutcomeBindingDocument | None = None


class ProcessingClaimOutcomesSettleDocument(RiverhogWorkflowDocument):
    model_config = ConfigDict(
        json_schema_extra={
            "if": {"properties": {"retirement_policy": {"const": "retain"}}},
            "then": {"properties": {"retirement_grace_seconds": {"const": 0}}},
        }
    )

    fence: int = Field(ge=1)
    retirement_policy: RetirementPolicy = "retain"
    retirement_grace_seconds: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_outcomes(self) -> Self:
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
    model_config = ConfigDict(
        json_schema_extra={
            "if": {"properties": {"retirement_policy": {"const": "retain"}}},
            "then": {"properties": {"retirement_grace_seconds": {"const": 0}}},
        }
    )

    execution_id: SHA256
    controller_evidence: dict[str, Any]
    controller_evidence_sha256: SHA256
    operation: OperationIdentityDocument
    inputs: ExactSetAuthorityDocument
    artifacts: ArtifactSetAuthorityDocument
    output_tags: ExactSetAuthorityDocument
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
            retirement_policy=self.retirement_policy,
            retirement_grace_seconds=self.retirement_grace_seconds,
        )
        return self


class ProcessingClaimOutcomeSettlementDocument(RiverhogWorkflowDocument):
    model_config = ConfigDict(
        json_schema_extra={
            "if": {"properties": {"retirement_policy": {"const": "retain"}}},
            "then": {"properties": {"retirement_grace_seconds": {"const": 0}}},
        }
    )

    outcomes: ExactSetAuthorityDocument
    retirement_policy: RetirementPolicy
    retirement_grace_seconds: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_retirement(self) -> Self:
        if self.retirement_policy == "retain" and self.retirement_grace_seconds:
            raise ValueError("retained collection work cannot declare retirement grace")
        return self


class RetirementClaimReferenceDocument(RiverhogWorkflowDocument):
    """Exact claim evidence authorizing one retirement deletion plan."""

    model_config = ConfigDict(
        json_schema_extra={
            "oneOf": [
                {
                    "properties": {
                        "execution_id": {"type": "string"},
                        "output_collection_id": {"type": "integer"},
                        "outcomes": {"type": "null"},
                    },
                    "required": ["execution_id", "output_collection_id"],
                },
                {
                    "properties": {
                        "execution_id": {"type": "null"},
                        "output_collection_id": {"type": "null"},
                        "outcomes": {"type": "object"},
                    },
                    "required": ["outcomes"],
                },
            ]
        }
    )

    claim_id: ProcessingClaimId
    fence: int = Field(ge=1)
    work_id: SHA256
    execution_id: SHA256 | None = None
    output_collection_id: CollectionId | None = None
    outcomes: ExactSetAuthorityDocument | None = None

    @model_validator(mode="after")
    def validate_settlement_form(self) -> Self:
        direct = self.execution_id is not None and self.output_collection_id is not None
        delegated = self.outcomes is not None
        if direct == delegated:
            raise ValueError("retirement claim must identify one direct or delegated settlement")
        if direct and self.outcomes is not None:
            raise ValueError("direct retirement claims cannot identify delegated outcomes")
        if delegated and (self.execution_id is not None or self.output_collection_id is not None):
            raise ValueError("delegated retirement claims cannot identify a direct output")
        return self


class ProcessingClaimDocument(RiverhogWorkflowDocument):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"state": {"enum": ["settled", "retiring", "released"]}}},
                    "then": {
                        "required": ["settled_at"],
                        "properties": {"settled_at": {"type": "string"}},
                    },
                    "else": {"properties": {"settled_at": {"type": "null"}}},
                },
                {
                    "if": {"properties": {"state": {"const": "abandoned"}}},
                    "then": {
                        "required": ["abandoned_at", "abandonment_reason"],
                        "properties": {
                            "abandoned_at": {"type": "string"},
                            "abandonment_reason": {"type": "string"},
                        },
                    },
                    "else": {
                        "properties": {
                            "abandoned_at": {"type": "null"},
                            "abandonment_reason": {"type": "null"},
                        }
                    },
                },
                {
                    "if": {"properties": {"state": {"const": "released"}}},
                    "then": {
                        "required": ["released_at"],
                        "properties": {"released_at": {"type": "string"}},
                    },
                    "else": {"properties": {"released_at": {"type": "null"}}},
                },
            ]
        }
    )

    format: Literal["riverhog-processing-claim/v1"]
    id: ProcessingClaimId
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
    output_collection_id: CollectionId | None = None
    work_document: dict[str, Any]
    work_document_sha256: SHA256
    inputs: ReceivingSetDocument
    plan: ProcessingClaimPlanDocument | None = None
    outcomes: OutcomeSetDocument
    outcome_settlement: ProcessingClaimOutcomeSettlementDocument | None = None

    @model_validator(mode="after")
    def validate_claim(self) -> Self:
        ProcessingClaimCreateDocument(
            work_id=self.work_id,
            work_document=self.work_document,
            work_document_sha256=self.work_document_sha256,
            purpose=self.purpose,
        )
        if self.outcome_settlement is not None:
            if self.outcomes.authority != self.outcome_settlement.outcomes:
                raise ValueError("processing outcome settlement identity is invalid")
            if self.plan is not None or self.state not in {"settled", "retiring", "released"}:
                raise ValueError("processing outcome settlement is inconsistent with claim state")
        settled = self.state in {"settled", "retiring", "released"}
        if settled != (self.settled_at is not None):
            raise ValueError("claim settlement timestamp is inconsistent with claim state")
        abandoned = self.state == "abandoned"
        if abandoned != (self.abandoned_at is not None):
            raise ValueError("claim abandonment timestamp is inconsistent with claim state")
        if abandoned != (self.abandonment_reason is not None):
            raise ValueError("claim abandonment reason is inconsistent with claim state")
        if self.abandonment_reason is not None and (
            self.abandonment_reason.strip() != self.abandonment_reason
        ):
            raise ValueError("claim abandonment reason must be canonical")
        released = self.state == "released"
        if released != (self.released_at is not None):
            raise ValueError("claim release timestamp is inconsistent with claim state")
        if self.plan is not None and self.outcomes.count:
            raise ValueError("direct collection work cannot retain delegated outcomes")
        if settled:
            if self.plan is not None:
                if self.output_collection_id is None or self.outcome_settlement is not None:
                    raise ValueError("direct claim settlement evidence is incomplete")
            elif (
                self.output_collection_id is not None
                or self.outcomes.authority is None
                or self.outcome_settlement is None
            ):
                raise ValueError("delegated claim settlement evidence is incomplete")
        elif self.output_collection_id is not None or self.outcome_settlement is not None:
            raise ValueError("unsettled claim cannot publish settlement evidence")
        return self


class ProcessingClaimFiltersDocument(RiverhogWorkflowDocument):
    state: ClaimState | None = None


class ProcessingClaimPageDocument(RiverhogWorkflowDocument):
    page: int = Field(ge=1)
    per_page: int = Field(ge=0)
    total: int = Field(ge=0)
    pages: int = Field(ge=0)
    sort: ProcessingClaimSort
    order: SortOrder
    filters: ProcessingClaimFiltersDocument
    claims: list[ProcessingClaimDocument]


class TransformCapabilityDocument(RiverhogWorkflowDocument):
    format: Literal["riverhog-transform-capability/v1"]
    id: str = Field(min_length=1, max_length=160)
    claim_id: ProcessingClaimId
    fence: int = Field(ge=1)
    audience: str = Field(pattern=r"^[a-z0-9][a-z0-9._:/-]{0,299}$")
    actions: list[CapabilityAction] = Field(
        min_length=1,
        json_schema_extra={
            "oneOf": [
                {"const": ["read-inputs"]},
                {"const": ["read-inputs", "write-output"]},
            ]
        },
    )
    state: Literal["receiving", "active"]
    principal_app: str = Field(min_length=1, max_length=300)
    expires_at: Timestamp
    artifacts: ArtifactReceivingSetDocument
    token: str = Field(pattern=r"^rhc_[A-Za-z0-9_-]+$")

    @model_validator(mode="after")
    def validate_capability(self) -> Self:
        TransformCapabilityCreateDocument(
            fence=self.fence,
            audience=self.audience,
            actions=self.actions,
        )
        return self


class CollectionDerivationResponseDocument(RiverhogWorkflowDocument):
    collection_id: CollectionId
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
    "DISPOSITION_BATCH_MAX",
    "ArtifactDispositionBatchDocument",
    "CapabilityAction",
    "ClaimState",
    "ArtifactDispositionDocument",
    "ArtifactDispositionOutputPageDocument",
    "ArtifactDispositionOutputBatchDocument",
    "ArtifactDispositionOutputDocument",
    "ArtifactDispositionPageDocument",
    "ArtifactDispositionSetDocument",
    "ArtifactDispositionSetIdentityDocument",
    "ArtifactReceivingSetDocument",
    "ArtifactSetAuthorityDocument",
    "CollectionArtifactBatchDocument",
    "CollectionArtifactIdentityDocument",
    "CollectionArtifactPageDocument",
    "CollectionDerivationDocument",
    "CollectionDerivationResponseDocument",
    "CollectionRootIdentityDocument",
    "CollectionRootBatchDocument",
    "CollectionRootPageDocument",
    "ExactSetAuthorityDocument",
    "OperationIdentityDocument",
    "OutputTagBatchDocument",
    "OutputTagPageDocument",
    "OutcomeSetDocument",
    "ProcessingClaimAbandonDocument",
    "ProcessingClaimCreateDocument",
    "ProcessingClaimDocument",
    "ProcessingClaimFenceDocument",
    "ProcessingClaimOutcomesSettleDocument",
    "ProcessingClaimId",
    "ProcessingClaimPageDocument",
    "ProcessingClaimPlanSealDocument",
    "ProcessingClaimRenewDocument",
    "ProcessingClaimRestartDocument",
    "ProcessingClaimSettleDocument",
    "ProcessingOutcomeBindingDocument",
    "ProcessingOutcomeIdentityDocument",
    "ProcessingOutcomePageDocument",
    "RecipeIdentityDocument",
    "ReceivingSetDocument",
    "RetirementClaimReferenceDocument",
    "RiverhogWorkflowDocument",
    "TransformCapabilityCreateDocument",
    "TransformCapabilityDocument",
    "WORK_DOCUMENT_MAX_BYTES",
    "WORKFLOW_SET_BATCH_MAX",
]

"""Official typed client methods for Riverhog collection work primitives."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from http_api_contracts import closed_literal_values
from pydantic import TypeAdapter, ValidationError
from riverhog_protocol import (
    ClaimState,
    CollectionId,
    ProcessingClaimId,
    ProcessingClaimSort,
    SortOrder,
)
from riverhog_protocol.collection_workflow_transport import (
    DISPOSITION_BATCH_MAX,
    WORKFLOW_SET_BATCH_MAX,
    ArtifactDispositionBatchDocument,
    ArtifactDispositionDocument,
    ArtifactDispositionOutputBatchDocument,
    ArtifactDispositionOutputDocument,
    ArtifactDispositionOutputPageDocument,
    ArtifactDispositionPageDocument,
    ArtifactDispositionSetDocument,
    ArtifactReceivingSetDocument,
    CapabilityAction,
    CollectionArtifactBatchDocument,
    CollectionArtifactIdentityDocument,
    CollectionArtifactPageDocument,
    CollectionDerivationDocument,
    CollectionDerivationResponseDocument,
    CollectionRootBatchDocument,
    CollectionRootIdentityDocument,
    CollectionRootPageDocument,
    OperationIdentityDocument,
    OutputTagBatchDocument,
    OutputTagPageDocument,
    ProcessingClaimAbandonDocument,
    ProcessingClaimCreateDocument,
    ProcessingClaimDocument,
    ProcessingClaimFenceDocument,
    ProcessingClaimOutcomesSettleDocument,
    ProcessingClaimPageDocument,
    ProcessingClaimPlanSealDocument,
    ProcessingClaimRenewDocument,
    ProcessingClaimRestartDocument,
    ProcessingClaimSettleDocument,
    ProcessingOutcomeBindingDocument,
    ProcessingOutcomeIdentityDocument,
    ProcessingOutcomePageDocument,
    ReceivingSetDocument,
    TransformCapabilityCreateDocument,
    TransformCapabilityDocument,
)
from riverhog_protocol.collection_workflows import RetirementPolicy
from riverhog_protocol.errors import BadRequest

RootInput = CollectionRootIdentityDocument | Mapping[str, Any]
ArtifactInput = CollectionArtifactIdentityDocument | Mapping[str, Any]
OutcomeInput = ProcessingOutcomeIdentityDocument | Mapping[str, Any]
DerivationInput = CollectionDerivationDocument | Mapping[str, Any]
DispositionInput = ArtifactDispositionDocument | Mapping[str, Any]
DispositionOutputInput = ArtifactDispositionOutputDocument | Mapping[str, Any]
_COLLECTION_ID: TypeAdapter[int] = TypeAdapter(CollectionId)
_PROCESSING_CLAIM_ID: TypeAdapter[str] = TypeAdapter(ProcessingClaimId)
_CLAIM_SORTS = closed_literal_values(ProcessingClaimSort)
_CLAIM_STATES = closed_literal_values(ClaimState)
_SORT_ORDERS = closed_literal_values(SortOrder)


def _one_of(value: str, allowed: frozenset[str], label: str) -> str:
    if value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise BadRequest(f"{label} must be one of: {choices}")
    return value


def _dump(value: object) -> dict[str, Any]:
    return value.model_dump(mode="json", exclude_none=True)  # type: ignore[attr-defined,no-any-return]


def _claim_id(value: str) -> str:
    try:
        return _PROCESSING_CLAIM_ID.validate_python(value, strict=True)
    except ValidationError as exc:
        raise BadRequest("processing claim id must be a lowercase SHA-256") from exc


def _chunks(values: Iterable[Any], *, maximum: int) -> Iterator[list[Any]]:
    chunk: list[Any] = []
    for value in values:
        chunk.append(value)
        if len(chunk) == maximum:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


class CollectionWorkflowMethods:
    """Mixin exposing Riverhog's generic collection-work API on ``ApiClient``."""

    if TYPE_CHECKING:

        def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]: ...
        def _stream_json_objects(
            self,
            path: str,
            *,
            query: Mapping[str, object],
            params: Mapping[str, object],
            schema_id: str,
        ) -> Any: ...

    def create_or_resume_processing_claim(
        self,
        *,
        work_id: str,
        work_document: Mapping[str, Any],
        work_document_sha256: str,
        inputs: Iterable[RootInput],
        lease_seconds: int = 1800,
        purpose: str = "collection-work/v1",
    ) -> ProcessingClaimDocument:
        request = ProcessingClaimCreateDocument(
            work_id=work_id,
            work_document=dict(work_document),
            work_document_sha256=work_document_sha256,
            lease_seconds=lease_seconds,
            purpose=purpose,
        )
        claim = ProcessingClaimDocument.model_validate(
            self._json("POST", "/v1/collection-processing-claims", json=_dump(request))
        )
        ordinal = claim.inputs.count
        if claim.inputs.state == "receiving":
            for chunk in _chunks(inputs, maximum=WORKFLOW_SET_BATCH_MAX):
                staged = self.append_processing_claim_inputs(
                    claim.id,
                    fence=claim.fence,
                    start_ordinal=ordinal,
                    inputs=chunk,
                )
                ordinal = staged.count
            self.seal_processing_claim_inputs(claim.id, fence=claim.fence)
            claim = self.get_processing_claim(claim.id)
        return claim

    def append_processing_claim_inputs(
        self,
        claim_id: ProcessingClaimId,
        *,
        fence: int,
        start_ordinal: int,
        inputs: Sequence[RootInput],
    ) -> ReceivingSetDocument:
        request = CollectionRootBatchDocument(
            fence=fence,
            start_ordinal=start_ordinal,
            inputs=[CollectionRootIdentityDocument.model_validate(item) for item in inputs],
        )
        return ReceivingSetDocument.model_validate(
            self._json(
                "PUT",
                f"/v1/collection-processing-claims/{_claim_id(claim_id)}/inputs",
                json=_dump(request),
            )
        )

    def seal_processing_claim_inputs(
        self,
        claim_id: ProcessingClaimId,
        *,
        fence: int,
    ) -> ReceivingSetDocument:
        return ReceivingSetDocument.model_validate(
            self._json(
                "POST",
                f"/v1/collection-processing-claims/{_claim_id(claim_id)}/inputs/seal",
                json=_dump(ProcessingClaimFenceDocument(fence=fence)),
            )
        )

    def list_processing_claim_inputs(
        self,
        claim_id: ProcessingClaimId,
        *,
        authority_sha256: str,
        start_ordinal: int = 0,
    ) -> CollectionRootPageDocument:
        return CollectionRootPageDocument.model_validate(
            self._json(
                "GET",
                f"/v1/collection-processing-claims/{_claim_id(claim_id)}/inputs",
                params={
                    "authority_sha256": authority_sha256,
                    "start_ordinal": start_ordinal,
                },
            )
        )

    def get_processing_claim(self, claim_id: ProcessingClaimId) -> ProcessingClaimDocument:
        return ProcessingClaimDocument.model_validate(
            self._json(
                "GET",
                f"/v1/collection-processing-claims/{_claim_id(claim_id)}",
            )
        )

    def list_processing_claims(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        state: ClaimState | None = None,
        sort: ProcessingClaimSort = "updated_at",
        order: SortOrder = "desc",
    ) -> ProcessingClaimPageDocument:
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "sort": _one_of(
                sort,
                _CLAIM_SORTS,
                "processing-claim sort",
            ),
            "order": _one_of(order, _SORT_ORDERS, "sort order"),
        }
        if state:
            params["state"] = _one_of(state, _CLAIM_STATES, "processing-claim state")
        return ProcessingClaimPageDocument.model_validate(
            self._json("GET", "/v1/collection-processing-claims", params=params)
        )

    def renew_processing_claim(
        self,
        claim_id: ProcessingClaimId,
        *,
        fence: int,
        lease_seconds: int = 1800,
    ) -> ProcessingClaimDocument:
        request = ProcessingClaimRenewDocument(fence=fence, lease_seconds=lease_seconds)
        return self._claim_response(claim_id, "renew", request)

    def restart_processing_claim(
        self,
        claim_id: ProcessingClaimId,
        *,
        fence: int,
        lease_seconds: int = 1800,
    ) -> ProcessingClaimDocument:
        request = ProcessingClaimRestartDocument(fence=fence, lease_seconds=lease_seconds)
        return self._claim_response(claim_id, "restart", request)

    def abandon_processing_claim(
        self,
        claim_id: ProcessingClaimId,
        *,
        fence: int,
        reason: str,
    ) -> ProcessingClaimDocument:
        return self._claim_response(
            claim_id,
            "abandon",
            ProcessingClaimAbandonDocument(fence=fence, reason=reason),
        )

    def seal_processing_claim_plan(
        self,
        claim_id: ProcessingClaimId,
        *,
        fence: int,
        execution_id: str,
        controller_evidence: Mapping[str, Any],
        controller_evidence_sha256: str,
        operation_id: str,
        operation_sha256: str,
        input_artifacts: Iterable[ArtifactInput],
        output_tags: Iterable[str],
        retirement_policy: RetirementPolicy = "retain",
        retirement_grace_seconds: int = 0,
    ) -> ProcessingClaimDocument:
        claim = self.get_processing_claim(claim_id)
        artifact_ordinal = claim.plan.artifacts.count if claim.plan is not None else 0
        for chunk in _chunks(input_artifacts, maximum=WORKFLOW_SET_BATCH_MAX):
            staged_artifacts = self.append_processing_claim_artifacts(
                claim_id,
                fence=fence,
                start_ordinal=artifact_ordinal,
                artifacts=chunk,
            )
            artifact_ordinal = staged_artifacts.count
        self.seal_processing_claim_artifacts(claim_id, fence=fence)
        tag_ordinal = claim.plan.output_tags.count if claim.plan is not None else 0
        for chunk in _chunks(output_tags, maximum=WORKFLOW_SET_BATCH_MAX):
            staged_tags = self.append_processing_claim_output_tags(
                claim_id,
                fence=fence,
                start_ordinal=tag_ordinal,
                tags=chunk,
            )
            tag_ordinal = staged_tags.count
        self.seal_processing_claim_output_tags(claim_id, fence=fence)
        request = ProcessingClaimPlanSealDocument(
            fence=fence,
            execution_id=execution_id,
            controller_evidence=dict(controller_evidence),
            controller_evidence_sha256=controller_evidence_sha256,
            operation=OperationIdentityDocument(id=operation_id, sha256=operation_sha256),
            retirement_policy=retirement_policy,
            retirement_grace_seconds=retirement_grace_seconds,
        )
        return self._claim_response(claim_id, "plan", request)

    def append_processing_claim_artifacts(
        self,
        claim_id: ProcessingClaimId,
        *,
        fence: int,
        start_ordinal: int,
        artifacts: Sequence[ArtifactInput],
    ) -> ArtifactReceivingSetDocument:
        request = CollectionArtifactBatchDocument(
            fence=fence,
            start_ordinal=start_ordinal,
            artifacts=[
                CollectionArtifactIdentityDocument.model_validate(item) for item in artifacts
            ],
        )
        return ArtifactReceivingSetDocument.model_validate(
            self._json(
                "PUT",
                f"/v1/collection-processing-claims/{_claim_id(claim_id)}/plan/artifacts",
                json=_dump(request),
            )
        )

    def seal_processing_claim_artifacts(
        self,
        claim_id: ProcessingClaimId,
        *,
        fence: int,
    ) -> ArtifactReceivingSetDocument:
        return ArtifactReceivingSetDocument.model_validate(
            self._json(
                "POST",
                f"/v1/collection-processing-claims/{_claim_id(claim_id)}/plan/artifacts/seal",
                json=_dump(ProcessingClaimFenceDocument(fence=fence)),
            )
        )

    def list_processing_claim_artifacts(
        self,
        claim_id: ProcessingClaimId,
        *,
        authority_sha256: str,
        start_ordinal: int = 0,
    ) -> CollectionArtifactPageDocument:
        return CollectionArtifactPageDocument.model_validate(
            self._json(
                "GET",
                f"/v1/collection-processing-claims/{_claim_id(claim_id)}/plan/artifacts",
                params={
                    "authority_sha256": authority_sha256,
                    "start_ordinal": start_ordinal,
                },
            )
        )

    def append_processing_claim_output_tags(
        self,
        claim_id: ProcessingClaimId,
        *,
        fence: int,
        start_ordinal: int,
        tags: Sequence[str],
    ) -> ReceivingSetDocument:
        request = OutputTagBatchDocument(
            fence=fence,
            start_ordinal=start_ordinal,
            tags=list(tags),
        )
        return ReceivingSetDocument.model_validate(
            self._json(
                "PUT",
                f"/v1/collection-processing-claims/{_claim_id(claim_id)}/plan/output-tags",
                json=_dump(request),
            )
        )

    def seal_processing_claim_output_tags(
        self,
        claim_id: ProcessingClaimId,
        *,
        fence: int,
    ) -> ReceivingSetDocument:
        return ReceivingSetDocument.model_validate(
            self._json(
                "POST",
                f"/v1/collection-processing-claims/{_claim_id(claim_id)}/plan/output-tags/seal",
                json=_dump(ProcessingClaimFenceDocument(fence=fence)),
            )
        )

    def list_processing_claim_output_tags(
        self,
        claim_id: ProcessingClaimId,
        *,
        authority_sha256: str,
        start_ordinal: int = 0,
    ) -> OutputTagPageDocument:
        return OutputTagPageDocument.model_validate(
            self._json(
                "GET",
                f"/v1/collection-processing-claims/{_claim_id(claim_id)}/plan/output-tags",
                params={
                    "authority_sha256": authority_sha256,
                    "start_ordinal": start_ordinal,
                },
            )
        )

    def create_transform_capability(
        self,
        claim_id: ProcessingClaimId,
        *,
        fence: int,
        audience: str,
        actions: Sequence[CapabilityAction] = ("read-inputs",),
        artifacts: Iterable[ArtifactInput],
        ttl_seconds: int = 900,
    ) -> TransformCapabilityDocument:
        request = TransformCapabilityCreateDocument(
            fence=fence,
            audience=audience,
            actions=list(actions),
            ttl_seconds=ttl_seconds,
        )
        capability = TransformCapabilityDocument.model_validate(
            self._json(
                "POST",
                f"/v1/collection-processing-claims/{_claim_id(claim_id)}/capabilities",
                json=_dump(request),
            )
        )
        ordinal = capability.artifacts.count
        for chunk in _chunks(artifacts, maximum=WORKFLOW_SET_BATCH_MAX):
            staged = self.append_transform_capability_artifacts(
                claim_id,
                capability.id,
                fence=fence,
                start_ordinal=ordinal,
                artifacts=chunk,
            )
            ordinal = staged.count
        sealed = self.seal_transform_capability_artifacts(
            claim_id,
            capability.id,
            fence=fence,
        )
        return capability.model_copy(update={"state": "active", "artifacts": sealed})

    def append_transform_capability_artifacts(
        self,
        claim_id: ProcessingClaimId,
        capability_id: str,
        *,
        fence: int,
        start_ordinal: int,
        artifacts: Sequence[ArtifactInput],
    ) -> ArtifactReceivingSetDocument:
        request = CollectionArtifactBatchDocument(
            fence=fence,
            start_ordinal=start_ordinal,
            artifacts=[
                CollectionArtifactIdentityDocument.model_validate(item) for item in artifacts
            ],
        )
        return ArtifactReceivingSetDocument.model_validate(
            self._json(
                "PUT",
                f"/v1/collection-processing-claims/{_claim_id(claim_id)}"
                f"/capabilities/{capability_id}/artifacts",
                json=_dump(request),
            )
        )

    def seal_transform_capability_artifacts(
        self,
        claim_id: ProcessingClaimId,
        capability_id: str,
        *,
        fence: int,
    ) -> ArtifactReceivingSetDocument:
        return ArtifactReceivingSetDocument.model_validate(
            self._json(
                "POST",
                f"/v1/collection-processing-claims/{_claim_id(claim_id)}"
                f"/capabilities/{capability_id}/artifacts/seal",
                json=_dump(ProcessingClaimFenceDocument(fence=fence)),
            )
        )

    def settle_processing_claim(
        self,
        claim_id: ProcessingClaimId,
        *,
        fence: int,
        output_collection_id: CollectionId,
        derivation: DerivationInput,
        outcome_claim_id: ProcessingClaimId | None = None,
        outcome_fence: int | None = None,
        outcome_id: str | None = None,
    ) -> ProcessingClaimDocument:
        outcome = None
        if any(item is not None for item in (outcome_claim_id, outcome_fence, outcome_id)):
            if outcome_claim_id is None or outcome_fence is None or outcome_id is None:
                raise ValueError("processing outcome binding is incomplete")
            outcome = ProcessingOutcomeBindingDocument(
                claim_id=outcome_claim_id,
                fence=outcome_fence,
                outcome_id=outcome_id,
            )
        request = ProcessingClaimSettleDocument(
            fence=fence,
            output_collection_id=output_collection_id,
            derivation=CollectionDerivationDocument.model_validate(derivation),
            outcome=outcome,
        )
        return self._claim_response(claim_id, "settle", request)

    def record_processing_claim_dispositions(
        self,
        claim_id: ProcessingClaimId,
        *,
        fence: int,
        dispositions: Sequence[DispositionInput],
    ) -> ArtifactDispositionSetDocument:
        values = list(dispositions)
        if not values or len(values) > DISPOSITION_BATCH_MAX:
            raise ValueError(f"disposition batch must contain 1 to {DISPOSITION_BATCH_MAX} facts")
        request = ArtifactDispositionBatchDocument(
            fence=fence,
            dispositions=[ArtifactDispositionDocument.model_validate(item) for item in values],
        )
        return ArtifactDispositionSetDocument.model_validate(
            self._json(
                "PUT",
                f"/v1/collection-processing-claims/{_claim_id(claim_id)}/derivation/dispositions",
                json=_dump(request),
            )
        )

    def list_processing_claim_dispositions(
        self,
        claim_id: ProcessingClaimId,
        *,
        authority_sha256: str,
        page: int = 1,
        per_page: int = 100,
    ) -> ArtifactDispositionPageDocument:
        return ArtifactDispositionPageDocument.model_validate(
            self._json(
                "GET",
                f"/v1/collection-processing-claims/{_claim_id(claim_id)}/derivation/dispositions",
                params={
                    "authority_sha256": authority_sha256,
                    "page": page,
                    "per_page": per_page,
                },
            )
        )

    def record_processing_claim_disposition_outputs(
        self,
        claim_id: ProcessingClaimId,
        *,
        fence: int,
        outputs: Sequence[DispositionOutputInput],
    ) -> ArtifactDispositionSetDocument:
        values = list(outputs)
        if not values or len(values) > DISPOSITION_BATCH_MAX:
            raise ValueError(
                f"disposition output batch must contain 1 to {DISPOSITION_BATCH_MAX} edges"
            )
        request = ArtifactDispositionOutputBatchDocument(
            fence=fence,
            outputs=[ArtifactDispositionOutputDocument.model_validate(item) for item in values],
        )
        return ArtifactDispositionSetDocument.model_validate(
            self._json(
                "PUT",
                f"/v1/collection-processing-claims/{_claim_id(claim_id)}/derivation/output-edges",
                json=_dump(request),
            )
        )

    def list_processing_claim_disposition_outputs(
        self,
        claim_id: ProcessingClaimId,
        *,
        authority_sha256: str,
        page: int = 1,
        per_page: int = 100,
    ) -> ArtifactDispositionOutputPageDocument:
        return ArtifactDispositionOutputPageDocument.model_validate(
            self._json(
                "GET",
                f"/v1/collection-processing-claims/{_claim_id(claim_id)}/derivation/output-edges",
                params={
                    "authority_sha256": authority_sha256,
                    "page": page,
                    "per_page": per_page,
                },
            )
        )

    def seal_processing_claim_dispositions(
        self,
        claim_id: ProcessingClaimId,
        *,
        fence: int,
    ) -> ArtifactDispositionSetDocument:
        request = ProcessingClaimFenceDocument(fence=fence)
        return ArtifactDispositionSetDocument.model_validate(
            self._json(
                "POST",
                f"/v1/collection-processing-claims/{_claim_id(claim_id)}/derivation/seal",
                json=_dump(request),
            )
        )

    def get_processing_claim_dispositions(
        self,
        claim_id: ProcessingClaimId,
    ) -> ArtifactDispositionSetDocument:
        return ArtifactDispositionSetDocument.model_validate(
            self._json(
                "GET",
                f"/v1/collection-processing-claims/{_claim_id(claim_id)}/derivation",
            )
        )

    def settle_processing_claim_outcomes(
        self,
        claim_id: ProcessingClaimId,
        *,
        fence: int,
        retirement_policy: RetirementPolicy = "retain",
        retirement_grace_seconds: int = 0,
    ) -> ProcessingClaimDocument:
        request = ProcessingClaimOutcomesSettleDocument(
            fence=fence,
            retirement_policy=retirement_policy,
            retirement_grace_seconds=retirement_grace_seconds,
        )
        return self._claim_response(claim_id, "outcomes/settle", request)

    def list_processing_claim_outcomes(
        self,
        claim_id: ProcessingClaimId,
        *,
        authority_sha256: str,
        start_ordinal: int = 0,
    ) -> ProcessingOutcomePageDocument:
        return ProcessingOutcomePageDocument.model_validate(
            self._json(
                "GET",
                f"/v1/collection-processing-claims/{_claim_id(claim_id)}/outcomes",
                params={
                    "authority_sha256": authority_sha256,
                    "start_ordinal": start_ordinal,
                },
            )
        )

    def begin_processing_claim_retirement(
        self,
        claim_id: ProcessingClaimId,
        *,
        fence: int,
    ) -> ProcessingClaimDocument:
        return self._claim_response(
            claim_id,
            "retirement",
            ProcessingClaimFenceDocument(fence=fence),
        )

    def release_processing_claim(
        self,
        claim_id: ProcessingClaimId,
        *,
        fence: int,
    ) -> ProcessingClaimDocument:
        return self._claim_response(
            claim_id,
            "release",
            ProcessingClaimFenceDocument(fence=fence),
        )

    def get_collection_derivation(
        self,
        collection_id: CollectionId,
    ) -> CollectionDerivationResponseDocument:
        try:
            normalized_id = _COLLECTION_ID.validate_python(collection_id)
        except ValidationError as exc:
            raise BadRequest("collection id must be a positive integer") from exc
        return CollectionDerivationResponseDocument.model_validate(
            self._json("GET", f"/v1/collections/{normalized_id}/derivation")
        )

    def _claim_response(
        self,
        claim_id: ProcessingClaimId,
        suffix: str,
        request: object,
    ) -> ProcessingClaimDocument:
        return ProcessingClaimDocument.model_validate(
            self._json(
                "POST",
                f"/v1/collection-processing-claims/{_claim_id(claim_id)}/{suffix}",
                json=_dump(request),
            )
        )


__all__ = ["CollectionWorkflowMethods"]

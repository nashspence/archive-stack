"""Official typed client methods for Riverhog collection work primitives."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
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
    CapabilityAction,
    CollectionArtifactIdentityDocument,
    CollectionDerivationDocument,
    CollectionDerivationResponseDocument,
    CollectionRootIdentityDocument,
    OperationIdentityDocument,
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
    TransformCapabilityCreateDocument,
    TransformCapabilityDocument,
)
from riverhog_protocol.collection_workflows import RetirementPolicy
from riverhog_protocol.errors import BadRequest

RootInput = CollectionRootIdentityDocument | Mapping[str, Any]
ArtifactInput = CollectionArtifactIdentityDocument | Mapping[str, Any]
OutcomeInput = ProcessingOutcomeIdentityDocument | Mapping[str, Any]
DerivationInput = CollectionDerivationDocument | Mapping[str, Any]
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
        inputs: Sequence[RootInput],
        lease_seconds: int = 1800,
        purpose: str = "collection-work/v1",
    ) -> ProcessingClaimDocument:
        request = ProcessingClaimCreateDocument(
            work_id=work_id,
            work_document=dict(work_document),
            work_document_sha256=work_document_sha256,
            inputs=[CollectionRootIdentityDocument.model_validate(item) for item in inputs],
            lease_seconds=lease_seconds,
            purpose=purpose,
        )
        return ProcessingClaimDocument.model_validate(
            self._json("POST", "/v1/collection-processing-claims", json=_dump(request))
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

    @contextmanager
    def stream_processing_claims(
        self,
        *,
        state: ClaimState | None = None,
        sort: ProcessingClaimSort = "updated_at",
        order: SortOrder = "desc",
    ) -> Iterator[Iterator[ProcessingClaimDocument]]:
        normalized_sort = _one_of(
            sort,
            _CLAIM_SORTS,
            "processing-claim sort",
        )
        normalized_order = _one_of(order, _SORT_ORDERS, "sort order")
        params: dict[str, object] = {"sort": normalized_sort, "order": normalized_order}
        if state:
            params["state"] = _one_of(state, _CLAIM_STATES, "processing-claim state")
        with self._stream_json_objects(
            "/v1/collection-processing-claims/stream",
            query={"state": state, "sort": normalized_sort, "order": normalized_order},
            params=params,
            schema_id="riverhog.collection-processing-claim/v1",
        ) as items:
            yield (ProcessingClaimDocument.model_validate(item) for item in items)

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
        input_artifacts: Sequence[ArtifactInput],
        output_tags: Sequence[str],
        retirement_policy: RetirementPolicy = "retain",
        retirement_grace_seconds: int = 0,
    ) -> ProcessingClaimDocument:
        request = ProcessingClaimPlanSealDocument(
            fence=fence,
            execution_id=execution_id,
            controller_evidence=dict(controller_evidence),
            controller_evidence_sha256=controller_evidence_sha256,
            operation=OperationIdentityDocument(id=operation_id, sha256=operation_sha256),
            input_artifacts=[
                CollectionArtifactIdentityDocument.model_validate(item) for item in input_artifacts
            ],
            output_tags=list(output_tags),
            retirement_policy=retirement_policy,
            retirement_grace_seconds=retirement_grace_seconds,
        )
        return self._claim_response(claim_id, "plan", request)

    def create_transform_capability(
        self,
        claim_id: ProcessingClaimId,
        *,
        fence: int,
        audience: str,
        actions: Sequence[CapabilityAction] = ("read-inputs",),
        artifacts: Sequence[ArtifactInput],
        ttl_seconds: int = 900,
    ) -> TransformCapabilityDocument:
        request = TransformCapabilityCreateDocument(
            fence=fence,
            audience=audience,
            actions=list(actions),
            artifacts=[
                CollectionArtifactIdentityDocument.model_validate(item) for item in artifacts
            ],
            ttl_seconds=ttl_seconds,
        )
        return TransformCapabilityDocument.model_validate(
            self._json(
                "POST",
                f"/v1/collection-processing-claims/{_claim_id(claim_id)}/capabilities",
                json=_dump(request),
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

    def settle_processing_claim_outcomes(
        self,
        claim_id: ProcessingClaimId,
        *,
        fence: int,
        outcomes: Sequence[OutcomeInput],
        retirement_policy: RetirementPolicy = "retain",
        retirement_grace_seconds: int = 0,
    ) -> ProcessingClaimDocument:
        request = ProcessingClaimOutcomesSettleDocument(
            fence=fence,
            outcomes=[ProcessingOutcomeIdentityDocument.model_validate(item) for item in outcomes],
            retirement_policy=retirement_policy,
            retirement_grace_seconds=retirement_grace_seconds,
        )
        return self._claim_response(claim_id, "outcomes/settle", request)

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

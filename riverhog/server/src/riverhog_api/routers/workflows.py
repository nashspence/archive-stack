from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Response
from http_api_contracts import operation_interface
from riverhog_protocol import (
    CollectionIdParameter,
    ProcessingClaimId,
    ProcessingClaimSort,
    SortOrder,
)
from riverhog_protocol.collection_workflow_transport import ClaimState
from riverhog_protocol.collection_workflows import (
    CollectionArtifactIdentity,
    CollectionProcessingOutcomeIdentity,
    CollectionRootIdentity,
)

from riverhog_api.auth import (
    CollectionTransformController,
    CollectionTransformExecutor,
    CollectionTransformLeaseManager,
)
from riverhog_api.complete_enumeration import (
    CompleteEnumerationResponse,
    bounded_list_operation,
    complete_enumeration_operation,
    complete_enumeration_response,
)
from riverhog_api.deps import ContainerDep
from riverhog_api.schemas.workflows import (
    CollectionDerivationOut,
    ProcessingClaimAbandonIn,
    ProcessingClaimCreateIn,
    ProcessingClaimFenceIn,
    ProcessingClaimOut,
    ProcessingClaimOutcomesSettleIn,
    ProcessingClaimPageOut,
    ProcessingClaimPlanSealIn,
    ProcessingClaimRenewIn,
    ProcessingClaimRestartIn,
    ProcessingClaimSettleIn,
    TransformCapabilityCreateIn,
    TransformCapabilityOut,
)

router = APIRouter(tags=["collection-workflows"])


@router.post(
    "/collection-processing-claims",
    response_model=ProcessingClaimOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def create_or_resume_processing_claim(
    request: ProcessingClaimCreateIn,
    container: ContainerDep,
    principal: CollectionTransformController,
) -> ProcessingClaimOut:
    return ProcessingClaimOut.model_validate(
        container.collection_workflows.create_or_resume_claim(
            work_id=request.work_id,
            work_document=request.work_document,
            work_document_sha256=request.work_document_sha256,
            inputs=tuple(
                CollectionRootIdentity(
                    collection_id=item.collection_id,
                    archive_root_sha256=item.archive_root_sha256,
                    content_identity=item.content_identity,
                )
                for item in request.inputs
            ),
            lease_seconds=request.lease_seconds,
            purpose=request.purpose,
            principal=principal,
        )
    )


@router.get(
    "/collection-processing-claims",
    response_model=ProcessingClaimPageOut,
    openapi_extra={
        **operation_interface("client-only-primitive"),
        **bounded_list_operation(paired_operation_id="stream_processing_claims"),
    },
)
def list_processing_claims(
    container: ContainerDep,
    principal: CollectionTransformController,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 25,
    state: ClaimState | None = None,
    sort: ProcessingClaimSort = "updated_at",
    order: SortOrder = "desc",
) -> ProcessingClaimPageOut:
    return ProcessingClaimPageOut.model_validate(
        container.collection_workflows.list_claims(
            page=page,
            per_page=per_page,
            state=state,
            sort=sort,
            order=order,
            principal=principal,
        )
    )


@router.get(
    "/collection-processing-claims/stream",
    response_class=CompleteEnumerationResponse,
    openapi_extra={
        **operation_interface("client-only-primitive"),
        **complete_enumeration_operation(
            paired_operation_id="list_processing_claims",
            item_type=ProcessingClaimOut,
            schema_id="riverhog.collection-processing-claim/v1",
        ),
    },
)
def stream_processing_claims(
    container: ContainerDep,
    principal: CollectionTransformController,
    state: ClaimState | None = None,
    sort: ProcessingClaimSort = "updated_at",
    order: SortOrder = "desc",
) -> Response:
    return complete_enumeration_response(
        container.collection_workflows.iter_claims(
            state=state, sort=sort, order=order, principal=principal
        ),
        query={"state": state, "sort": sort, "order": order},
        item_type=ProcessingClaimOut,
        schema_id="riverhog.collection-processing-claim/v1",
    )


@router.get(
    "/collection-processing-claims/{claim_id}",
    response_model=ProcessingClaimOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def get_processing_claim(
    claim_id: ProcessingClaimId,
    container: ContainerDep,
    principal: CollectionTransformController,
) -> ProcessingClaimOut:
    return ProcessingClaimOut.model_validate(
        container.collection_workflows.get_claim(claim_id, principal=principal)
    )


@router.post(
    "/collection-processing-claims/{claim_id}/renew",
    response_model=ProcessingClaimOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def renew_processing_claim(
    claim_id: ProcessingClaimId,
    request: ProcessingClaimRenewIn,
    container: ContainerDep,
    principal: CollectionTransformLeaseManager,
) -> ProcessingClaimOut:
    return ProcessingClaimOut.model_validate(
        container.collection_workflows.renew_claim(
            claim_id,
            fence=request.fence,
            lease_seconds=request.lease_seconds,
            principal=principal,
        )
    )


@router.post(
    "/collection-processing-claims/{claim_id}/restart",
    response_model=ProcessingClaimOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def restart_processing_claim(
    claim_id: ProcessingClaimId,
    request: ProcessingClaimRestartIn,
    container: ContainerDep,
    principal: CollectionTransformController,
) -> ProcessingClaimOut:
    return ProcessingClaimOut.model_validate(
        container.collection_workflows.restart_claim(
            claim_id,
            fence=request.fence,
            lease_seconds=request.lease_seconds,
            principal=principal,
        )
    )


@router.post(
    "/collection-processing-claims/{claim_id}/abandon",
    response_model=ProcessingClaimOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def abandon_processing_claim(
    claim_id: ProcessingClaimId,
    request: ProcessingClaimAbandonIn,
    container: ContainerDep,
    principal: CollectionTransformController,
) -> ProcessingClaimOut:
    return ProcessingClaimOut.model_validate(
        container.collection_workflows.abandon_claim(
            claim_id,
            fence=request.fence,
            reason=request.reason,
            principal=principal,
        )
    )


@router.post(
    "/collection-processing-claims/{claim_id}/plan",
    response_model=ProcessingClaimOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def seal_processing_claim_plan(
    claim_id: ProcessingClaimId,
    request: ProcessingClaimPlanSealIn,
    container: ContainerDep,
    principal: CollectionTransformExecutor,
) -> ProcessingClaimOut:
    return ProcessingClaimOut.model_validate(
        container.collection_workflows.seal_claim_plan(
            claim_id,
            fence=request.fence,
            execution_id=request.execution_id,
            controller_evidence=request.controller_evidence,
            controller_evidence_sha256=request.controller_evidence_sha256,
            operation_id=request.operation.id,
            operation_sha256=request.operation.sha256,
            input_artifacts=tuple(
                CollectionArtifactIdentity.from_mapping(item.model_dump(mode="json"))
                for item in request.input_artifacts
            ),
            output_tags=request.output_tags,
            retirement_policy=request.retirement_policy,
            retirement_grace_seconds=request.retirement_grace_seconds,
            principal=principal,
        )
    )


@router.post(
    "/collection-processing-claims/{claim_id}/capabilities",
    response_model=TransformCapabilityOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def create_transform_capability(
    claim_id: ProcessingClaimId,
    request: TransformCapabilityCreateIn,
    container: ContainerDep,
    principal: CollectionTransformExecutor,
) -> TransformCapabilityOut:
    return TransformCapabilityOut.model_validate(
        container.collection_workflows.issue_capability(
            claim_id,
            fence=request.fence,
            audience=request.audience,
            actions=request.actions,
            artifacts=tuple(
                CollectionArtifactIdentity.from_mapping(item.model_dump(mode="json"))
                for item in request.artifacts
            ),
            ttl_seconds=request.ttl_seconds,
            principal=principal,
        )
    )


@router.post(
    "/collection-processing-claims/{claim_id}/settle",
    response_model=ProcessingClaimOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def settle_processing_claim(
    claim_id: ProcessingClaimId,
    request: ProcessingClaimSettleIn,
    container: ContainerDep,
    principal: CollectionTransformController,
) -> ProcessingClaimOut:
    return ProcessingClaimOut.model_validate(
        container.collection_workflows.settle_claim(
            claim_id,
            fence=request.fence,
            output_collection_id=request.output_collection_id,
            derivation=request.derivation.model_dump(mode="json"),
            outcome_claim_id=(request.outcome.claim_id if request.outcome is not None else None),
            outcome_fence=(request.outcome.fence if request.outcome is not None else None),
            outcome_id=(request.outcome.outcome_id if request.outcome is not None else None),
            principal=principal,
        )
    )


@router.post(
    "/collection-processing-claims/{claim_id}/outcomes/settle",
    response_model=ProcessingClaimOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def settle_processing_claim_outcomes(
    claim_id: ProcessingClaimId,
    request: ProcessingClaimOutcomesSettleIn,
    container: ContainerDep,
    principal: CollectionTransformController,
) -> ProcessingClaimOut:
    return ProcessingClaimOut.model_validate(
        container.collection_workflows.settle_claim_outcomes(
            claim_id,
            fence=request.fence,
            outcomes=tuple(
                CollectionProcessingOutcomeIdentity.from_mapping(item.model_dump(mode="json"))
                for item in request.outcomes
            ),
            retirement_policy=request.retirement_policy,
            retirement_grace_seconds=request.retirement_grace_seconds,
            principal=principal,
        )
    )


@router.post(
    "/collection-processing-claims/{claim_id}/retirement",
    response_model=ProcessingClaimOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def begin_processing_claim_retirement(
    claim_id: ProcessingClaimId,
    request: ProcessingClaimFenceIn,
    container: ContainerDep,
    principal: CollectionTransformController,
) -> ProcessingClaimOut:
    return ProcessingClaimOut.model_validate(
        container.collection_workflows.begin_retirement(
            claim_id,
            fence=request.fence,
            principal=principal,
        )
    )


@router.post(
    "/collection-processing-claims/{claim_id}/release",
    response_model=ProcessingClaimOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def release_processing_claim(
    claim_id: ProcessingClaimId,
    request: ProcessingClaimFenceIn,
    container: ContainerDep,
    principal: CollectionTransformController,
) -> ProcessingClaimOut:
    return ProcessingClaimOut.model_validate(
        container.collection_workflows.release_claim(
            claim_id,
            fence=request.fence,
            principal=principal,
        )
    )


@router.get(
    "/collections/{collection_id}/derivation",
    response_model=CollectionDerivationOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def get_collection_derivation(
    collection_id: CollectionIdParameter,
    container: ContainerDep,
    principal: CollectionTransformController,
) -> CollectionDerivationOut:
    return CollectionDerivationOut.model_validate(
        container.collection_workflows.get_derivation(
            collection_id,
            principal=principal,
        )
    )

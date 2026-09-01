from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from http_api_contracts import (
    exact_authority_page_operation,
    mutable_browse_operation,
    operation_interface,
)
from riverhog_protocol import (
    CollectionIdParameter,
    ProcessingClaimId,
    ProcessingClaimSort,
    SortOrder,
)
from riverhog_protocol.collection_workflow_transport import ClaimState
from riverhog_protocol.collection_workflows import (
    ArtifactDisposition,
    ArtifactDispositionOutput,
    CollectionArtifactIdentity,
    CollectionRootIdentity,
)

from riverhog_api.auth import (
    CollectionTransformController,
    CollectionTransformExecutor,
    CollectionTransformLeaseManager,
)
from riverhog_api.browse import (
    BrowsePageTokenQuery,
    canonical_selectors,
    page_payload,
    page_position,
)
from riverhog_api.deps import ContainerDep
from riverhog_api.schemas.workflows import (
    ArtifactDispositionBatchIn,
    ArtifactDispositionOutputBatchIn,
    ArtifactDispositionOutputPageOut,
    ArtifactDispositionPageOut,
    ArtifactDispositionSetOut,
    ArtifactReceivingSetOut,
    CollectionArtifactBatchIn,
    CollectionArtifactPageOut,
    CollectionDerivationOut,
    CollectionRootBatchIn,
    CollectionRootPageOut,
    OutputTagBatchIn,
    OutputTagPageOut,
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
    ProcessingOutcomePageOut,
    ReceivingSetOut,
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
            lease_seconds=request.lease_seconds,
            purpose=request.purpose,
            principal=principal,
        )
    )


@router.put(
    "/collection-processing-claims/{claim_id}/inputs",
    response_model=ReceivingSetOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def append_processing_claim_inputs(
    claim_id: ProcessingClaimId,
    request: CollectionRootBatchIn,
    container: ContainerDep,
    principal: CollectionTransformController,
) -> ReceivingSetOut:
    return ReceivingSetOut.model_validate(
        container.collection_workflows.append_claim_inputs(
            claim_id,
            fence=request.fence,
            start_ordinal=request.start_ordinal,
            inputs=tuple(
                CollectionRootIdentity.from_mapping(item.model_dump(mode="json"))
                for item in request.inputs
            ),
            principal=principal,
        )
    )


@router.post(
    "/collection-processing-claims/{claim_id}/inputs/seal",
    response_model=ReceivingSetOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def seal_processing_claim_inputs(
    claim_id: ProcessingClaimId,
    request: ProcessingClaimFenceIn,
    container: ContainerDep,
    principal: CollectionTransformController,
) -> ReceivingSetOut:
    return ReceivingSetOut.model_validate(
        container.collection_workflows.seal_claim_inputs(
            claim_id,
            fence=request.fence,
            principal=principal,
        )
    )


@router.get(
    "/collection-processing-claims/{claim_id}/inputs",
    response_model=CollectionRootPageOut,
    openapi_extra={
        **operation_interface("client-only-primitive"),
        **exact_authority_page_operation(
            authority="processing-claim-inputs",
            authority_parameter="authority_sha256",
            cursor_parameter="start_ordinal",
            fixed_limit=128,
        ),
    },
)
def list_processing_claim_inputs(
    claim_id: ProcessingClaimId,
    container: ContainerDep,
    principal: CollectionTransformLeaseManager,
    authority_sha256: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")],
    start_ordinal: Annotated[int, Query(ge=0)] = 0,
) -> CollectionRootPageOut:
    return CollectionRootPageOut.model_validate(
        container.collection_workflows.list_claim_inputs(
            claim_id,
            authority_sha256=authority_sha256,
            start_ordinal=start_ordinal,
            principal=principal,
        )
    )


@router.put(
    "/collection-processing-claims/{claim_id}/plan/artifacts",
    response_model=ArtifactReceivingSetOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def append_processing_claim_artifacts(
    claim_id: ProcessingClaimId,
    request: CollectionArtifactBatchIn,
    container: ContainerDep,
    principal: CollectionTransformController,
) -> ArtifactReceivingSetOut:
    return ArtifactReceivingSetOut.model_validate(
        container.collection_workflows.append_claim_artifacts(
            claim_id,
            fence=request.fence,
            start_ordinal=request.start_ordinal,
            artifacts=tuple(
                CollectionArtifactIdentity.from_mapping(item.model_dump(mode="json"))
                for item in request.artifacts
            ),
            principal=principal,
        )
    )


@router.post(
    "/collection-processing-claims/{claim_id}/plan/artifacts/seal",
    response_model=ArtifactReceivingSetOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def seal_processing_claim_artifacts(
    claim_id: ProcessingClaimId,
    request: ProcessingClaimFenceIn,
    container: ContainerDep,
    principal: CollectionTransformController,
) -> ArtifactReceivingSetOut:
    return ArtifactReceivingSetOut.model_validate(
        container.collection_workflows.seal_claim_artifacts(
            claim_id,
            fence=request.fence,
            principal=principal,
        )
    )


@router.get(
    "/collection-processing-claims/{claim_id}/plan/artifacts",
    response_model=CollectionArtifactPageOut,
    openapi_extra={
        **operation_interface("client-only-primitive"),
        **exact_authority_page_operation(
            authority="processing-claim-artifacts",
            authority_parameter="authority_sha256",
            cursor_parameter="start_ordinal",
            fixed_limit=128,
        ),
    },
)
def list_processing_claim_artifacts(
    claim_id: ProcessingClaimId,
    container: ContainerDep,
    principal: CollectionTransformLeaseManager,
    authority_sha256: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")],
    start_ordinal: Annotated[int, Query(ge=0)] = 0,
) -> CollectionArtifactPageOut:
    return CollectionArtifactPageOut.model_validate(
        container.collection_workflows.list_claim_artifacts(
            claim_id,
            authority_sha256=authority_sha256,
            start_ordinal=start_ordinal,
            principal=principal,
        )
    )


@router.put(
    "/collection-processing-claims/{claim_id}/plan/output-tags",
    response_model=ReceivingSetOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def append_processing_claim_output_tags(
    claim_id: ProcessingClaimId,
    request: OutputTagBatchIn,
    container: ContainerDep,
    principal: CollectionTransformController,
) -> ReceivingSetOut:
    return ReceivingSetOut.model_validate(
        container.collection_workflows.append_claim_output_tags(
            claim_id,
            fence=request.fence,
            start_ordinal=request.start_ordinal,
            tags=request.tags,
            principal=principal,
        )
    )


@router.post(
    "/collection-processing-claims/{claim_id}/plan/output-tags/seal",
    response_model=ReceivingSetOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def seal_processing_claim_output_tags(
    claim_id: ProcessingClaimId,
    request: ProcessingClaimFenceIn,
    container: ContainerDep,
    principal: CollectionTransformController,
) -> ReceivingSetOut:
    return ReceivingSetOut.model_validate(
        container.collection_workflows.seal_claim_output_tags(
            claim_id,
            fence=request.fence,
            principal=principal,
        )
    )


@router.get(
    "/collection-processing-claims/{claim_id}/plan/output-tags",
    response_model=OutputTagPageOut,
    openapi_extra={
        **operation_interface("client-only-primitive"),
        **exact_authority_page_operation(
            authority="processing-claim-output-tags",
            authority_parameter="authority_sha256",
            cursor_parameter="start_ordinal",
            fixed_limit=128,
        ),
    },
)
def list_processing_claim_output_tags(
    claim_id: ProcessingClaimId,
    container: ContainerDep,
    principal: CollectionTransformLeaseManager,
    authority_sha256: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")],
    start_ordinal: Annotated[int, Query(ge=0)] = 0,
) -> OutputTagPageOut:
    return OutputTagPageOut.model_validate(
        container.collection_workflows.list_claim_output_tags(
            claim_id,
            authority_sha256=authority_sha256,
            start_ordinal=start_ordinal,
            principal=principal,
        )
    )


@router.get(
    "/collection-processing-claims",
    response_model=ProcessingClaimPageOut,
    openapi_extra={
        **operation_interface("client-only-primitive"),
        **mutable_browse_operation(),
    },
)
def list_processing_claims(
    container: ContainerDep,
    principal: CollectionTransformController,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
    page_token: BrowsePageTokenQuery = None,
    state: ClaimState | None = None,
    sort: ProcessingClaimSort = "updated_at",
    order: SortOrder = "desc",
) -> ProcessingClaimPageOut:
    selectors = canonical_selectors(state=state, sort=sort, order=order)
    position = page_position(
        container,
        principal=principal,
        operation="list_processing_claims",
        page_token=page_token,
        selectors=selectors,
    )
    return ProcessingClaimPageOut.model_validate(
        page_payload(
            container.collection_workflows.list_claims(
                page_size=page_size,
                position=position,
                state=state,
                sort=sort,
                order=order,
                principal=principal,
            ),
            container=container,
            principal=principal,
            operation="list_processing_claims",
            selectors=selectors,
        )
    )


@router.get(
    "/collection-processing-claims/{claim_id}",
    response_model=ProcessingClaimOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def get_processing_claim(
    claim_id: ProcessingClaimId,
    container: ContainerDep,
    principal: CollectionTransformLeaseManager,
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
            ttl_seconds=request.ttl_seconds,
            principal=principal,
        )
    )


@router.put(
    "/collection-processing-claims/{claim_id}/capabilities/{capability_id}/artifacts",
    response_model=ArtifactReceivingSetOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def append_transform_capability_artifacts(
    claim_id: ProcessingClaimId,
    capability_id: str,
    request: CollectionArtifactBatchIn,
    container: ContainerDep,
    principal: CollectionTransformController,
) -> ArtifactReceivingSetOut:
    return ArtifactReceivingSetOut.model_validate(
        container.collection_workflows.append_capability_artifacts(
            claim_id,
            capability_id,
            fence=request.fence,
            start_ordinal=request.start_ordinal,
            artifacts=tuple(
                CollectionArtifactIdentity.from_mapping(item.model_dump(mode="json"))
                for item in request.artifacts
            ),
            principal=principal,
        )
    )


@router.post(
    "/collection-processing-claims/{claim_id}/capabilities/{capability_id}/artifacts/seal",
    response_model=ArtifactReceivingSetOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def seal_transform_capability_artifacts(
    claim_id: ProcessingClaimId,
    capability_id: str,
    request: ProcessingClaimFenceIn,
    container: ContainerDep,
    principal: CollectionTransformController,
) -> ArtifactReceivingSetOut:
    return ArtifactReceivingSetOut.model_validate(
        container.collection_workflows.seal_capability_artifacts(
            claim_id,
            capability_id,
            fence=request.fence,
            principal=principal,
        )
    )


@router.put(
    "/collection-processing-claims/{claim_id}/derivation/dispositions",
    response_model=ArtifactDispositionSetOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def record_processing_claim_dispositions(
    claim_id: ProcessingClaimId,
    request: ArtifactDispositionBatchIn,
    container: ContainerDep,
    principal: CollectionTransformLeaseManager,
) -> ArtifactDispositionSetOut:
    return ArtifactDispositionSetOut.model_validate(
        container.collection_workflows.record_dispositions(
            claim_id,
            fence=request.fence,
            dispositions=tuple(
                ArtifactDisposition.from_mapping(item.model_dump(mode="json", exclude_none=True))
                for item in request.dispositions
            ),
            principal=principal,
        )
    )


@router.get(
    "/collection-processing-claims/{claim_id}/derivation/dispositions",
    response_model=ArtifactDispositionPageOut,
    openapi_extra={
        **operation_interface("client-only-primitive"),
        **exact_authority_page_operation(
            authority="processing-claim-dispositions",
            authority_parameter="authority_sha256",
            cursor_parameter="start_ordinal",
            fixed_limit=128,
        ),
    },
)
def list_processing_claim_dispositions(
    claim_id: ProcessingClaimId,
    container: ContainerDep,
    principal: CollectionTransformController,
    authority_sha256: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")],
    start_ordinal: Annotated[int, Query(ge=0)] = 0,
) -> ArtifactDispositionPageOut:
    return ArtifactDispositionPageOut.model_validate(
        container.collection_workflows.list_dispositions(
            claim_id,
            authority_sha256=authority_sha256,
            start_ordinal=start_ordinal,
            principal=principal,
        )
    )


@router.put(
    "/collection-processing-claims/{claim_id}/derivation/output-edges",
    response_model=ArtifactDispositionSetOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def record_processing_claim_disposition_outputs(
    claim_id: ProcessingClaimId,
    request: ArtifactDispositionOutputBatchIn,
    container: ContainerDep,
    principal: CollectionTransformLeaseManager,
) -> ArtifactDispositionSetOut:
    return ArtifactDispositionSetOut.model_validate(
        container.collection_workflows.record_disposition_outputs(
            claim_id,
            fence=request.fence,
            outputs=tuple(
                ArtifactDispositionOutput.from_mapping(item.model_dump(mode="json"))
                for item in request.outputs
            ),
            principal=principal,
        )
    )


@router.get(
    "/collection-processing-claims/{claim_id}/derivation/output-edges",
    response_model=ArtifactDispositionOutputPageOut,
    openapi_extra={
        **operation_interface("client-only-primitive"),
        **exact_authority_page_operation(
            authority="processing-claim-disposition-outputs",
            authority_parameter="authority_sha256",
            cursor_parameter="start_ordinal",
            fixed_limit=128,
        ),
    },
)
def list_processing_claim_disposition_outputs(
    claim_id: ProcessingClaimId,
    container: ContainerDep,
    principal: CollectionTransformController,
    authority_sha256: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")],
    start_ordinal: Annotated[int, Query(ge=0)] = 0,
) -> ArtifactDispositionOutputPageOut:
    return ArtifactDispositionOutputPageOut.model_validate(
        container.collection_workflows.list_disposition_outputs(
            claim_id,
            authority_sha256=authority_sha256,
            start_ordinal=start_ordinal,
            principal=principal,
        )
    )


@router.post(
    "/collection-processing-claims/{claim_id}/derivation/seal",
    response_model=ArtifactDispositionSetOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def seal_processing_claim_dispositions(
    claim_id: ProcessingClaimId,
    request: ProcessingClaimFenceIn,
    container: ContainerDep,
    principal: CollectionTransformLeaseManager,
) -> ArtifactDispositionSetOut:
    return ArtifactDispositionSetOut.model_validate(
        container.collection_workflows.seal_disposition_set(
            claim_id,
            fence=request.fence,
            principal=principal,
        )
    )


@router.get(
    "/collection-processing-claims/{claim_id}/derivation",
    response_model=ArtifactDispositionSetOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def get_processing_claim_dispositions(
    claim_id: ProcessingClaimId,
    container: ContainerDep,
    principal: CollectionTransformLeaseManager,
) -> ArtifactDispositionSetOut:
    return ArtifactDispositionSetOut.model_validate(
        container.collection_workflows.get_disposition_set(
            claim_id,
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
            retirement_policy=request.retirement_policy,
            retirement_grace_seconds=request.retirement_grace_seconds,
            principal=principal,
        )
    )


@router.get(
    "/collection-processing-claims/{claim_id}/outcomes",
    response_model=ProcessingOutcomePageOut,
    openapi_extra={
        **operation_interface("client-only-primitive"),
        **exact_authority_page_operation(
            authority="processing-claim-outcomes",
            authority_parameter="authority_sha256",
            cursor_parameter="start_ordinal",
            fixed_limit=128,
        ),
    },
)
def list_processing_claim_outcomes(
    claim_id: ProcessingClaimId,
    container: ContainerDep,
    principal: CollectionTransformController,
    authority_sha256: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")],
    start_ordinal: Annotated[int, Query(ge=0)] = 0,
) -> ProcessingOutcomePageOut:
    return ProcessingOutcomePageOut.model_validate(
        container.collection_workflows.list_claim_outcomes(
            claim_id,
            authority_sha256=authority_sha256,
            start_ordinal=start_ordinal,
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

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from http_api_contracts import operation_interface

from riverhog_api.auth import CollectionTransformManager
from riverhog_api.deps import ContainerDep
from riverhog_api.schemas.workflows import (
    CollectionDerivationOut,
    ProcessingClaimCreateIn,
    ProcessingClaimFenceIn,
    ProcessingClaimOut,
    ProcessingClaimPageOut,
    ProcessingClaimRenewIn,
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
    principal: CollectionTransformManager,
) -> ProcessingClaimOut:
    return ProcessingClaimOut.model_validate(
        container.collection_workflows.create_or_resume_claim(
            input_collection_ids=request.input_collection_ids,
            recipe_id=request.recipe.id,
            recipe_revision=request.recipe.revision,
            recipe_sha256=request.recipe.sha256,
            operation_id=request.operation.id,
            operation_sha256=request.operation.sha256,
            effective_intent=request.effective_intent,
            output_tags=request.output_tags,
            retirement_policy=request.retirement_policy,
            retirement_grace_seconds=request.retirement_grace_seconds,
            lease_seconds=request.lease_seconds,
            purpose=request.purpose,
            principal=principal,
        )
    )


@router.get(
    "/collection-processing-claims",
    response_model=ProcessingClaimPageOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def list_processing_claims(
    container: ContainerDep,
    principal: CollectionTransformManager,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 25,
    state: str | None = None,
    sort: str = "updated_at",
    order: str = "desc",
    all_items: Annotated[bool, Query(alias="all")] = False,
) -> ProcessingClaimPageOut:
    return ProcessingClaimPageOut.model_validate(
        container.collection_workflows.list_claims(
            page=page,
            per_page=per_page,
            state=state,
            sort=sort,
            order=order,
            all_items=all_items,
            principal=principal,
        )
    )


@router.get(
    "/collection-processing-claims/{claim_id}",
    response_model=ProcessingClaimOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def get_processing_claim(
    claim_id: str,
    container: ContainerDep,
    principal: CollectionTransformManager,
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
    claim_id: str,
    request: ProcessingClaimRenewIn,
    container: ContainerDep,
    principal: CollectionTransformManager,
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
    "/collection-processing-claims/{claim_id}/capabilities",
    response_model=TransformCapabilityOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def create_transform_capability(
    claim_id: str,
    request: TransformCapabilityCreateIn,
    container: ContainerDep,
    principal: CollectionTransformManager,
) -> TransformCapabilityOut:
    return TransformCapabilityOut.model_validate(
        container.collection_workflows.issue_capability(
            claim_id,
            fence=request.fence,
            actions=request.actions,
            ttl_seconds=request.ttl_seconds,
            principal=principal,
        )
    )


@router.post(
    "/collection-processing-claims/{claim_id}/settle",
    response_model=ProcessingClaimOut,
    openapi_extra=operation_interface("service-internal"),
)
def settle_processing_claim(
    claim_id: str,
    request: ProcessingClaimSettleIn,
    container: ContainerDep,
    principal: CollectionTransformManager,
) -> ProcessingClaimOut:
    return ProcessingClaimOut.model_validate(
        container.collection_workflows.settle_claim(
            claim_id,
            fence=request.fence,
            output_collection_id=request.output_collection_id,
            derivation=request.derivation,
            principal=principal,
        )
    )


@router.post(
    "/collection-processing-claims/{claim_id}/retirement",
    response_model=ProcessingClaimOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def begin_processing_claim_retirement(
    claim_id: str,
    request: ProcessingClaimFenceIn,
    container: ContainerDep,
    principal: CollectionTransformManager,
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
    claim_id: str,
    request: ProcessingClaimFenceIn,
    container: ContainerDep,
    principal: CollectionTransformManager,
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
    collection_id: int,
    container: ContainerDep,
    principal: CollectionTransformManager,
) -> CollectionDerivationOut:
    return CollectionDerivationOut.model_validate(
        container.collection_workflows.get_derivation(
            collection_id,
            principal=principal,
        )
    )

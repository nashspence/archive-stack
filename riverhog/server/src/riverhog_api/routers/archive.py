from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from http_api_contracts import mutable_browse_operation
from riverhog_core.app_permissions import ARCHIVES_MANAGE
from riverhog_protocol import (
    ArchiveCopySort,
    ArchiveCopyState,
    ArchiveStoreName,
    ArchiveStoreSort,
    CollectionIdParameter,
    SortOrder,
)

from riverhog_api.auth import ArchiveManager, ArchiveReader
from riverhog_api.browse import canonical_selectors, page_payload, page_position
from riverhog_api.deps import ContainerDep
from riverhog_api.mappers import map_archive_store, map_archive_store_list
from riverhog_api.schemas.archive import (
    ArchiveCopyJobListOut,
    ArchiveCopyJobOut,
    ArchiveCopyRetirementPlanOut,
    ArchiveCopyRetirementRequest,
    ArchiveCopyRetirementResultOut,
    CreateArchiveCopyRequest,
    RetireArchiveCopyRequest,
)
from riverhog_api.schemas.archive_stores import ArchiveStoreListOut, ArchiveStoreOut

router = APIRouter(tags=["archive"])


@router.post("/archive/copies", response_model=ArchiveCopyJobOut)
def create_or_resume_archive_copy(
    request: CreateArchiveCopyRequest,
    container: ContainerDep,
    principal: ArchiveManager,
) -> ArchiveCopyJobOut:
    container.collection_access.require(principal, ARCHIVES_MANAGE, request.collection_id)
    return ArchiveCopyJobOut.model_validate(
        container.archive_copies.create_or_resume(
            request.collection_id,
            destination_store=request.destination_store,
            source_store=request.source_store,
            initiator=principal,
            event_context=request.event_context,
        )
    )


@router.get(
    "/archive/copies",
    response_model=ArchiveCopyJobListOut,
    openapi_extra=mutable_browse_operation(),
)
def list_archive_copy_jobs(
    container: ContainerDep,
    principal: ArchiveManager,
    page_size: int = Query(25, ge=1, le=100),
    page_token: str | None = Query(None),
    q: str | None = Query(None),
    state: Annotated[ArchiveCopyState | None, Query()] = None,
    sort: Annotated[ArchiveCopySort, Query()] = "requested_at",
    order: Annotated[SortOrder, Query()] = "desc",
) -> ArchiveCopyJobListOut:
    selectors = canonical_selectors(q=q, state=state, sort=sort, order=order)
    position = page_position(
        container,
        principal=principal,
        operation="list_archive_copy_jobs",
        page_token=page_token,
        selectors=selectors,
    )
    return ArchiveCopyJobListOut.model_validate(
        page_payload(
            container.archive_copies.list(
                page_size=page_size,
                position=position,
                q=q,
                state=state,
                sort=sort,
                order=order,
                principal=principal,
            ),
            container=container,
            principal=principal,
            operation="list_archive_copy_jobs",
            selectors=selectors,
        )
    )


@router.delete(
    "/archive/copies/{collection_id}/{destination_store}",
    response_model=ArchiveCopyJobOut,
)
def cancel_archive_copy_job(
    collection_id: CollectionIdParameter,
    destination_store: ArchiveStoreName,
    container: ContainerDep,
    principal: ArchiveManager,
) -> ArchiveCopyJobOut:
    container.collection_access.require(principal, ARCHIVES_MANAGE, collection_id)
    return ArchiveCopyJobOut.model_validate(
        container.archive_copies.cancel(
            collection_id,
            destination_store=destination_store,
            principal=principal,
        )
    )


@router.get(
    "/archive/copies/{collection_id}/{destination_store}",
    response_model=ArchiveCopyJobOut,
)
def get_archive_copy_job(
    collection_id: CollectionIdParameter,
    destination_store: ArchiveStoreName,
    container: ContainerDep,
    principal: ArchiveManager,
) -> ArchiveCopyJobOut:
    container.collection_access.require(principal, ARCHIVES_MANAGE, collection_id)
    return ArchiveCopyJobOut.model_validate(
        container.archive_copies.get(
            collection_id,
            destination_store=destination_store,
            principal=principal,
        )
    )


@router.post(
    "/archive/copies/retirement-plan",
    response_model=ArchiveCopyRetirementPlanOut,
)
def plan_archive_copy_retirement(
    request: ArchiveCopyRetirementRequest,
    container: ContainerDep,
    principal: ArchiveManager,
) -> ArchiveCopyRetirementPlanOut:
    container.collection_access.require(principal, ARCHIVES_MANAGE, request.collection_id)
    return ArchiveCopyRetirementPlanOut.model_validate(
        container.archive_copy_retirements.plan(
            request.collection_id,
            store=request.store,
        )
    )


@router.post(
    "/archive/copies/retire",
    response_model=ArchiveCopyRetirementResultOut,
)
def retire_archive_copy(
    request: RetireArchiveCopyRequest,
    container: ContainerDep,
    principal: ArchiveManager,
) -> ArchiveCopyRetirementResultOut:
    container.collection_access.require(principal, ARCHIVES_MANAGE, request.collection_id)
    return ArchiveCopyRetirementResultOut.model_validate(
        container.archive_copy_retirements.retire(
            request.collection_id,
            store=request.store,
            challenge=request.challenge,
        )
    )


@router.get(
    "/archive/stores",
    response_model=ArchiveStoreListOut,
    openapi_extra=mutable_browse_operation(),
)
def list_archive_stores(
    container: ContainerDep,
    principal: ArchiveReader,
    page_size: int = Query(25, ge=1, le=100),
    page_token: str | None = Query(None),
    q: str | None = Query(None),
    sort: Annotated[ArchiveStoreSort, Query()] = "store",
    order: Annotated[SortOrder, Query()] = "asc",
) -> ArchiveStoreListOut:
    selectors = canonical_selectors(q=q, sort=sort, order=order)
    position = page_position(
        container,
        principal=principal,
        operation="list_archive_stores",
        page_token=page_token,
        selectors=selectors,
    )
    payload = map_archive_store_list(
        container.archive_stores.list(
            page_size=page_size,
            position=position,
            q=q,
            sort=sort,
            order=order,
            principal=principal,
        )
    )
    return ArchiveStoreListOut.model_validate(
        page_payload(
            payload,
            container=container,
            principal=principal,
            operation="list_archive_stores",
            selectors=selectors,
        )
    )


@router.get("/archive/stores/{store}", response_model=ArchiveStoreOut)
def get_archive_store(
    store: ArchiveStoreName,
    container: ContainerDep,
    principal: ArchiveReader,
) -> ArchiveStoreOut:
    return ArchiveStoreOut.model_validate(
        map_archive_store(container.archive_stores.get(store, principal=principal))
    )

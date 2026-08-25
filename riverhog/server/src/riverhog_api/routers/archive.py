from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from http_api_contracts import error_responses
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


@router.get("/archive/copies", response_model=ArchiveCopyJobListOut)
def list_archive_copy_jobs(
    container: ContainerDep,
    principal: ArchiveManager,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    q: str | None = Query(None),
    state: Annotated[ArchiveCopyState | None, Query()] = None,
    sort: Annotated[ArchiveCopySort, Query()] = "requested_at",
    order: Annotated[SortOrder, Query()] = "desc",
    all_items: bool = Query(False, alias="all"),
) -> ArchiveCopyJobListOut:
    return ArchiveCopyJobListOut.model_validate(
        container.archive_copies.list(
            page=page,
            per_page=per_page,
            q=q,
            state=state,
            sort=sort,
            order=order,
            all_items=all_items,
            principal=principal,
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
    responses=error_responses("service_unavailable"),
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


@router.get("/archive/stores", response_model=ArchiveStoreListOut)
def list_archive_stores(
    container: ContainerDep,
    principal: ArchiveReader,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    q: str | None = Query(None),
    sort: Annotated[ArchiveStoreSort, Query()] = "store",
    order: Annotated[SortOrder, Query()] = "asc",
    all_items: bool = Query(False, alias="all"),
) -> ArchiveStoreListOut:
    payload = container.archive_stores.list(
        page=page,
        per_page=per_page,
        q=q,
        sort=sort,
        order=order,
        all_items=all_items,
        principal=principal,
    )
    return ArchiveStoreListOut.model_validate(map_archive_store_list(payload))


@router.get("/archive/stores/{store}", response_model=ArchiveStoreOut)
def get_archive_store(
    store: ArchiveStoreName,
    container: ContainerDep,
    principal: ArchiveReader,
) -> ArchiveStoreOut:
    return ArchiveStoreOut.model_validate(
        map_archive_store(container.archive_stores.get(store, principal=principal))
    )

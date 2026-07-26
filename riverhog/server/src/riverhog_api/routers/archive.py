from __future__ import annotations

from fastapi import APIRouter, Query
from riverhog_core.app_permissions import ARCHIVES_MANAGE, ARCHIVES_READ

from riverhog_api.auth import ArchiveManager, ArchiveReader
from riverhog_api.deps import ContainerDep
from riverhog_api.mappers import map_archive_usage_report
from riverhog_api.schemas.archive import (
    ArchiveCopyJobOut,
    ArchiveCopyRetirementPlanOut,
    ArchiveCopyRetirementRequest,
    ArchiveCopyRetirementResultOut,
    CreateArchiveCopyRequest,
    RetireArchiveCopyRequest,
)
from riverhog_api.schemas.archive_usage import ArchiveUsageReportOut

router = APIRouter(tags=["archive"])


@router.post("/archive/copies", response_model=ArchiveCopyJobOut)
def create_archive_copy(
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


@router.get("/archive", response_model=ArchiveUsageReportOut)
def get_archive_report(
    container: ContainerDep,
    principal: ArchiveReader,
    collection: int | None = Query(None),
) -> ArchiveUsageReportOut:
    if collection is not None:
        container.collection_access.require(principal, ARCHIVES_READ, collection)
    payload = container.archive_reporting.get_report(
        collection=collection,
        principal=principal,
    )
    return ArchiveUsageReportOut.model_validate(map_archive_usage_report(payload))

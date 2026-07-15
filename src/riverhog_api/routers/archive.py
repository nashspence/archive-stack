from __future__ import annotations

from fastapi import APIRouter, Query

from riverhog_api.deps import ContainerDep
from riverhog_api.mappers import map_archive_usage_report
from riverhog_api.schemas.archive import ArchiveCopyJobOut, CreateArchiveCopyRequest
from riverhog_api.schemas.archive_usage import ArchiveUsageReportOut

router = APIRouter(tags=["archive"])


@router.post("/archive/copies", response_model=ArchiveCopyJobOut)
def create_archive_copy(
    request: CreateArchiveCopyRequest,
    container: ContainerDep,
) -> ArchiveCopyJobOut:
    return ArchiveCopyJobOut.model_validate(
        container.archive_copies.create_or_resume(
            request.collection_id,
            destination_store=request.destination_store,
            source_store=request.source_store,
        )
    )


@router.get("/archive", response_model=ArchiveUsageReportOut)
def get_archive_report(
    container: ContainerDep,
    collection: str | None = Query(None),
) -> ArchiveUsageReportOut:
    payload = container.archive_reporting.get_report(
        collection=collection,
    )
    return ArchiveUsageReportOut.model_validate(map_archive_usage_report(payload))

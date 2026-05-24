from __future__ import annotations

from fastapi import APIRouter, Query

from riverhog_api.deps import ContainerDep
from riverhog_api.mappers import map_glacier_usage_report
from riverhog_api.schemas.glacier import GlacierUsageReportOut

router = APIRouter(tags=["glacier"])


@router.get("/glacier", response_model=GlacierUsageReportOut)
def get_glacier_report(
    container: ContainerDep,
    collection: str | None = Query(None),
) -> GlacierUsageReportOut:
    payload = container.glacier_reporting.get_report(
        collection=collection,
    )
    return GlacierUsageReportOut.model_validate(map_glacier_usage_report(payload))

from __future__ import annotations

from fastapi import APIRouter, Query

from arc_api.deps import ContainerDep
from arc_api.mappers import map_glacier_usage_report
from arc_api.schemas.glacier import GlacierUsageReportOut

router = APIRouter(tags=["glacier"])


@router.get("/glacier", response_model=GlacierUsageReportOut)
def get_glacier_report(
    container: ContainerDep,
    image_id: str | None = Query(None),
    collection: str | None = Query(None),
) -> GlacierUsageReportOut:
    payload = container.glacier_reporting.get_report(
        image_id=image_id,
        collection=collection,
    )
    return GlacierUsageReportOut.model_validate(map_glacier_usage_report(payload))

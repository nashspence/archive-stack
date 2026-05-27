from __future__ import annotations

from fastapi import APIRouter, Query

from riverhog_api.deps import ContainerDep
from riverhog_api.schemas.dashboard import DashboardCollectionsResponse

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard/collections", response_model=DashboardCollectionsResponse)
def list_dashboard_collections(
    container: ContainerDep,
    q: str | None = Query(None),
) -> DashboardCollectionsResponse:
    payload = container.collections.list_dashboard_collections(q=q)
    return DashboardCollectionsResponse.model_validate(payload)

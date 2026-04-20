from __future__ import annotations

from fastapi import APIRouter, Depends

from arc_api.deps import ServiceContainer, get_container
from arc_api.schemas.plan import PlanResponse

router = APIRouter(tags=["plan"])


@router.get("/plan", response_model=PlanResponse)
def get_plan(container: ServiceContainer = Depends(get_container)) -> PlanResponse:
    payload = container.planning.get_plan()
    return PlanResponse.model_validate(payload)

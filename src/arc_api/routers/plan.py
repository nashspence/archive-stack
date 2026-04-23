from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query

from arc_api.deps import ContainerDep
from arc_api.schemas.plan import PlanResponse

router = APIRouter(tags=["plan"])


@router.get("/plan", response_model=PlanResponse)
def get_plan(
    container: ContainerDep,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    sort: Literal["fill", "bytes", "files", "collections", "candidate_id"] = Query("fill"),
    order: Literal["asc", "desc"] = Query("desc"),
    q: str | None = Query(None),
    collection: str | None = Query(None),
    iso_ready: bool | None = Query(None),
) -> PlanResponse:
    payload = container.planning.get_plan(
        page=page,
        per_page=per_page,
        sort=sort,
        order=order,
        q=q,
        collection=collection,
        iso_ready=iso_ready,
    )
    return PlanResponse.model_validate(payload)

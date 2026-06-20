from __future__ import annotations

from fastapi import APIRouter, Query

from riverhog_api.deps import ContainerDep
from riverhog_api.mappers import map_pin
from riverhog_api.schemas.pins import (
    PinRequest,
    PinResponse,
    PinsResponse,
    PinSummaryOut,
    ReleaseRequest,
    ReleaseResponse,
)

router = APIRouter(tags=["pins"])


@router.post("/pin", response_model=PinResponse)
def pin_target(
    request: PinRequest,
    container: ContainerDep,
) -> PinResponse:
    payload = container.pins.pin(request.target)
    return PinResponse.model_validate(payload)


@router.post("/release", response_model=ReleaseResponse)
def release_target(
    request: ReleaseRequest,
    container: ContainerDep,
) -> ReleaseResponse:
    payload = container.pins.release(request.target)
    return ReleaseResponse.model_validate(payload)


@router.get("/pins", response_model=PinsResponse)
def list_pins(
    container: ContainerDep,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
) -> PinsResponse:
    summary = container.pins.list_pins(page=page, per_page=per_page)
    return PinsResponse(
        page=summary.page,
        per_page=summary.per_page,
        total=summary.total,
        pages=summary.pages,
        pins=[PinSummaryOut.model_validate(map_pin(item)) for item in summary.pins],
    )

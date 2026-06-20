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
    pins = [PinSummaryOut.model_validate(map_pin(item)) for item in container.pins.list_pins()]
    total = len(pins)
    pages = (total + per_page - 1) // per_page if total else 0
    start = (page - 1) * per_page
    stop = start + per_page
    return PinsResponse(
        page=page,
        per_page=per_page,
        total=total,
        pages=pages,
        pins=pins[start:stop],
    )

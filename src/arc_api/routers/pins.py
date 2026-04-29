from __future__ import annotations

from fastapi import APIRouter

from arc_api.deps import ContainerDep
from arc_api.mappers import map_pin
from arc_api.schemas.pins import (
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
def list_pins(container: ContainerDep) -> PinsResponse:
    pins = [PinSummaryOut.model_validate(map_pin(item)) for item in container.pins.list_pins()]
    return PinsResponse(pins=pins)

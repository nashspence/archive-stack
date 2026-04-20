from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from arc_api.deps import ServiceContainer, get_container
from arc_api.mappers import map_copy
from arc_api.schemas.images import CopyOut, ImageSummaryResponse, RegisterCopyRequest, RegisterCopyResponse
from arc_core.iso.streaming import IsoStream

router = APIRouter(tags=["images"])


@router.get("/images/{image_id}", response_model=ImageSummaryResponse)
def get_image(image_id: str, container: ServiceContainer = Depends(get_container)) -> ImageSummaryResponse:
    payload = container.planning.get_image(image_id)
    return ImageSummaryResponse.model_validate(payload)


@router.get("/images/{image_id}/iso")
async def get_iso(image_id: str, container: ServiceContainer = Depends(get_container)) -> StreamingResponse:
    stream = container.planning.get_iso_stream(image_id)
    if hasattr(stream, "__await__"):
        stream = await stream
    if isinstance(stream, IsoStream):
        return StreamingResponse(stream.body, media_type=stream.media_type, headers=stream.headers)
    return StreamingResponse(stream, media_type="application/octet-stream")


@router.post("/images/{image_id}/copies", response_model=RegisterCopyResponse)
def register_copy(
    image_id: str,
    request: RegisterCopyRequest,
    container: ServiceContainer = Depends(get_container),
) -> RegisterCopyResponse:
    summary = container.copies.register(image_id=image_id, copy_id=request.id, location=request.location)
    return RegisterCopyResponse.model_validate({"copy": CopyOut.model_validate(map_copy(summary)).model_dump()})

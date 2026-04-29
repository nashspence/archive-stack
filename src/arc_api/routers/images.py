from __future__ import annotations

from collections.abc import AsyncIterable, Iterable
from inspect import isawaitable
from typing import Literal

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from arc_api.deps import ContainerDep
from arc_api.mappers import map_copy
from arc_api.schemas.images import (
    CopyOut,
    FinalizedImageSummaryResponse,
    ListCopiesResponse,
    ListImagesResponse,
    RegisterCopyRequest,
    RegisterCopyResponse,
    UpdateCopyRequest,
)
from arc_core.iso.streaming import IsoStream

router = APIRouter(tags=["images"])


@router.get("/images", response_model=ListImagesResponse)
def list_images(
    container: ContainerDep,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    sort: Literal["finalized_at", "bytes", "physical_copies_registered"] = Query("finalized_at"),
    order: Literal["asc", "desc"] = Query("desc"),
    q: str | None = Query(None),
    collection: str | None = Query(None),
    has_copies: bool | None = Query(None),
) -> ListImagesResponse:
    payload = container.planning.list_images(
        page=page,
        per_page=per_page,
        sort=sort,
        order=order,
        q=q,
        collection=collection,
        has_copies=has_copies,
    )
    return ListImagesResponse.model_validate(payload)


@router.get("/images/{image_id}", response_model=FinalizedImageSummaryResponse)
def get_image(image_id: str, container: ContainerDep) -> FinalizedImageSummaryResponse:
    payload = container.planning.get_image(image_id)
    return FinalizedImageSummaryResponse.model_validate(payload)


@router.post(
    "/plan/candidates/{candidate_id}/finalize", response_model=FinalizedImageSummaryResponse
)
def finalize_image(candidate_id: str, container: ContainerDep) -> FinalizedImageSummaryResponse:
    payload = container.planning.finalize_image(candidate_id)
    return FinalizedImageSummaryResponse.model_validate(payload)


@router.get("/images/{image_id}/iso")
async def get_iso(image_id: str, container: ContainerDep) -> StreamingResponse:
    stream_or_awaitable = container.planning.get_iso_stream(image_id)
    if isawaitable(stream_or_awaitable):
        stream = await stream_or_awaitable
    else:
        stream = stream_or_awaitable
    if isinstance(stream, IsoStream):
        return StreamingResponse(stream.body, media_type=stream.media_type, headers=stream.headers)
    body: AsyncIterable[bytes] | Iterable[bytes] = stream
    return StreamingResponse(body, media_type="application/octet-stream")


@router.post("/images/{image_id}/copies", response_model=RegisterCopyResponse)
def register_copy(
    image_id: str,
    request: RegisterCopyRequest,
    container: ContainerDep,
) -> RegisterCopyResponse:
    summary = container.copies.register(
        image_id=image_id, copy_id=request.copy_id, location=request.location
    )
    return RegisterCopyResponse.model_validate(
        {"copy": CopyOut.model_validate(map_copy(summary)).model_dump()}
    )


@router.get("/images/{image_id}/copies", response_model=ListCopiesResponse)
def list_copies(
    image_id: str,
    container: ContainerDep,
) -> ListCopiesResponse:
    copies = container.copies.list_for_image(image_id)
    return ListCopiesResponse.model_validate({"copies": [map_copy(copy) for copy in copies]})


@router.patch("/images/{image_id}/copies/{copy_id}", response_model=RegisterCopyResponse)
def update_copy(
    image_id: str,
    copy_id: str,
    request: UpdateCopyRequest,
    container: ContainerDep,
) -> RegisterCopyResponse:
    summary = container.copies.update(
        image_id=image_id,
        copy_id=copy_id,
        location=request.location,
        state=request.state,
        verification_state=request.verification_state,
    )
    return RegisterCopyResponse.model_validate(
        {"copy": CopyOut.model_validate(map_copy(summary)).model_dump()}
    )

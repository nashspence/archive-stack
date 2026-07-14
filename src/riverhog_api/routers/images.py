from __future__ import annotations

from collections.abc import AsyncIterable, Iterable
from inspect import isawaitable
from typing import Literal

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from riverhog_api.deps import ContainerDep
from riverhog_api.mappers import map_disc
from riverhog_api.schemas.images import (
    DiscOut,
    FinalizedImageSummaryResponse,
    ImageDiscsResponse,
    ListDiscsResponse,
    ListImagesResponse,
    RegisterDiscRequest,
    RegisterDiscResponse,
    UpdateDiscRequest,
)
from riverhog_core.iso.streaming import IsoStream

router = APIRouter(tags=["images"])


@router.get("/discs", response_model=ListDiscsResponse)
def list_discs(
    container: ContainerDep,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    sort: Literal["disc_id", "image_id", "state", "verification_state", "location"] = Query(
        "disc_id"
    ),
    order: Literal["asc", "desc"] = Query("asc"),
    q: str | None = Query(None),
    image_id: str | None = Query(None),
) -> ListDiscsResponse:
    payload = container.discs.list_discs(
        page=page,
        per_page=per_page,
        sort=sort,
        order=order,
        q=q,
        image_id=image_id,
    )
    return ListDiscsResponse.model_validate(payload)


@router.get("/discs/{disc_id}", response_model=DiscOut)
def get_disc(disc_id: str, container: ContainerDep) -> DiscOut:
    payload = container.discs.get_disc(disc_id)
    return DiscOut.model_validate(payload)


@router.get("/images", response_model=ListImagesResponse)
def list_images(
    container: ContainerDep,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    sort: Literal["finalized_at", "bytes", "discs_registered"] = Query("finalized_at"),
    order: Literal["asc", "desc"] = Query("desc"),
    q: str | None = Query(None),
    collection: str | None = Query(None),
    has_discs: bool | None = Query(None),
) -> ListImagesResponse:
    payload = container.planning.list_images(
        page=page,
        per_page=per_page,
        sort=sort,
        order=order,
        q=q,
        collection=collection,
        has_discs=has_discs,
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


@router.post("/images/{image_id}/discs", response_model=RegisterDiscResponse)
def register_disc(
    image_id: str,
    request: RegisterDiscRequest,
    container: ContainerDep,
) -> RegisterDiscResponse:
    summary = container.discs.register(
        image_id=image_id, disc_id=request.disc_id, location=request.location
    )
    return RegisterDiscResponse(disc=DiscOut.model_validate(map_disc(summary)))


@router.get("/images/{image_id}/discs", response_model=ImageDiscsResponse)
def list_image_discs(
    image_id: str,
    container: ContainerDep,
) -> ImageDiscsResponse:
    discs = container.discs.list_for_image(image_id)
    return ImageDiscsResponse.model_validate({"discs": [map_disc(disc) for disc in discs]})


@router.post(
    "/images/{image_id}/discs/{disc_id}/label-needed",
    response_model=RegisterDiscResponse,
)
def notify_disc_label_needed(
    image_id: str,
    disc_id: str,
    container: ContainerDep,
) -> RegisterDiscResponse:
    summary = container.discs.notify_label_needed(image_id=image_id, disc_id=disc_id)
    return RegisterDiscResponse(disc=DiscOut.model_validate(map_disc(summary)))


@router.patch("/images/{image_id}/discs/{disc_id}", response_model=RegisterDiscResponse)
def update_disc(
    image_id: str,
    disc_id: str,
    request: UpdateDiscRequest,
    container: ContainerDep,
) -> RegisterDiscResponse:
    summary = container.discs.update(
        image_id=image_id,
        disc_id=disc_id,
        location=request.location,
        state=request.state,
        verification_state=request.verification_state,
    )
    return RegisterDiscResponse(disc=DiscOut.model_validate(map_disc(summary)))

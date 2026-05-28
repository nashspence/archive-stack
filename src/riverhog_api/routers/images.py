from __future__ import annotations

from collections.abc import AsyncIterable, Iterable
from dataclasses import dataclass
from inspect import isawaitable
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import StreamingResponse

from riverhog_api.deps import ContainerDep
from riverhog_api.mappers import map_copy
from riverhog_api.schemas.images import (
    CopyOut,
    FinalizedImageSummaryResponse,
    ListCopiesResponse,
    ListImagesResponse,
    RegisterCopyRequest,
    RegisterCopyResponse,
    UpdateCopyRequest,
)
from riverhog_core.iso.streaming import IsoStream

router = APIRouter(tags=["images"])


@dataclass(frozen=True, slots=True)
class _ByteRange:
    start: int
    end: int
    total: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1


def _content_length(headers: dict[str, str] | None) -> int | None:
    if not headers:
        return None
    raw_value = headers.get("Content-Length") or headers.get("content-length")
    if raw_value is None:
        return None
    try:
        value = int(raw_value)
    except ValueError:
        return None
    return value if value >= 0 else None


def _range_not_satisfiable(total: int | None) -> HTTPException:
    content_range = f"bytes */{total}" if total is not None else "bytes */*"
    return HTTPException(
        status_code=416,
        detail="requested byte range is not satisfiable",
        headers={"Content-Range": content_range, "Accept-Ranges": "bytes"},
    )


def _parse_range_header(range_header: str, *, total: int) -> _ByteRange:
    normalized = range_header.strip()
    if not normalized.startswith("bytes=") or "," in normalized:
        raise _range_not_satisfiable(total)
    start_raw, separator, end_raw = normalized.removeprefix("bytes=").partition("-")
    if separator != "-":
        raise _range_not_satisfiable(total)
    try:
        if start_raw == "":
            suffix_length = int(end_raw)
            if suffix_length <= 0:
                raise ValueError
            start = max(total - suffix_length, 0)
            end = total - 1
        else:
            start = int(start_raw)
            end = int(end_raw) if end_raw else total - 1
    except ValueError as exc:
        raise _range_not_satisfiable(total) from exc
    if start < 0 or start >= total or end < start:
        raise _range_not_satisfiable(total)
    return _ByteRange(start=start, end=min(end, total - 1), total=total)


async def _range_body(body: AsyncIterable[bytes], byte_range: _ByteRange) -> AsyncIterable[bytes]:
    skip = byte_range.start
    remaining = byte_range.length
    try:
        async for chunk in body:
            if not chunk:
                continue
            if skip:
                if len(chunk) <= skip:
                    skip -= len(chunk)
                    continue
                chunk = chunk[skip:]
                skip = 0
            if len(chunk) > remaining:
                yield chunk[:remaining]
                return
            yield chunk
            remaining -= len(chunk)
            if remaining <= 0:
                return
    finally:
        close = getattr(body, "aclose", None)
        if callable(close):
            await close()


def _iso_streaming_response(
    stream: IsoStream,
    *,
    range_header: str | None,
) -> StreamingResponse:
    headers = dict(stream.headers or {})
    total = _content_length(headers)
    if total is not None:
        headers.setdefault("Accept-Ranges", "bytes")
    if range_header is None:
        return StreamingResponse(
            stream.body,
            media_type=stream.media_type,
            headers=headers,
            status_code=stream.status_code,
        )
    if total is None or total <= 0:
        raise _range_not_satisfiable(total)
    byte_range = _parse_range_header(range_header, total=total)
    headers["Accept-Ranges"] = "bytes"
    headers["Content-Length"] = str(byte_range.length)
    headers["Content-Range"] = f"bytes {byte_range.start}-{byte_range.end}/{byte_range.total}"
    return StreamingResponse(
        _range_body(stream.body, byte_range),
        media_type=stream.media_type,
        headers=headers,
        status_code=206,
    )


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
async def get_iso(
    image_id: str,
    container: ContainerDep,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
) -> StreamingResponse:
    stream_or_awaitable = container.planning.get_iso_stream(image_id)
    if isawaitable(stream_or_awaitable):
        stream = await stream_or_awaitable
    else:
        stream = stream_or_awaitable
    if isinstance(stream, IsoStream):
        return _iso_streaming_response(stream, range_header=range_header)
    if range_header is not None:
        raise _range_not_satisfiable(None)
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


@router.post(
    "/images/{image_id}/copies/{copy_id}/label-needed",
    response_model=RegisterCopyResponse,
)
def notify_copy_label_needed(
    image_id: str,
    copy_id: str,
    container: ContainerDep,
) -> RegisterCopyResponse:
    summary = container.copies.notify_label_needed(image_id=image_id, copy_id=copy_id)
    return RegisterCopyResponse.model_validate(
        {"copy": CopyOut.model_validate(map_copy(summary)).model_dump()}
    )


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

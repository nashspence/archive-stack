from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import StreamingResponse
from riverhog_protocol.errors import BadRequest

from riverhog_api.auth import RetrievalManager
from riverhog_api.deps import ContainerDep
from riverhog_api.schemas.retrieval import (
    CreateRetrievalJobRequest,
    RetrievalJobOut,
    RetrievalPlanOut,
    RetrievalPlanRequest,
)

router = APIRouter(tags=["retrieval"])


def _files(request: RetrievalPlanRequest) -> list[tuple[str, str]]:
    return [(item.collection_id, item.path) for item in request.files]


@router.post("/retrieval-plans", response_model=RetrievalPlanOut)
def plan_retrieval(
    request: RetrievalPlanRequest,
    _principal: RetrievalManager,
    container: ContainerDep,
) -> RetrievalPlanOut:
    payload = container.retrieval.plan(
        _files(request),
        lease=(
            timedelta(seconds=request.lease_seconds) if request.lease_seconds is not None else None
        ),
    )
    return RetrievalPlanOut.model_validate(payload)


@router.post("/retrieval-jobs", response_model=RetrievalJobOut)
def create_retrieval_job(
    request: CreateRetrievalJobRequest,
    principal: RetrievalManager,
    container: ContainerDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> RetrievalJobOut:
    plan_etag = (if_match or "").strip().strip('"')
    payload = container.retrieval.create(
        app=principal.app,
        key_id=principal.key_id,
        files=_files(request),
        plan_etag=plan_etag,
        lease=(
            timedelta(seconds=request.lease_seconds) if request.lease_seconds is not None else None
        ),
        event_context=request.event_context,
    )
    return RetrievalJobOut.model_validate(payload)


@router.get("/retrieval-jobs/{job_id}", response_model=RetrievalJobOut)
def get_retrieval_job(
    job_id: str,
    principal: RetrievalManager,
    container: ContainerDep,
) -> RetrievalJobOut:
    return RetrievalJobOut.model_validate(container.retrieval.get(app=principal.app, job_id=job_id))


@router.delete("/retrieval-jobs/{job_id}", response_model=RetrievalJobOut)
def cancel_retrieval_job(
    job_id: str,
    principal: RetrievalManager,
    container: ContainerDep,
) -> RetrievalJobOut:
    return RetrievalJobOut.model_validate(
        container.retrieval.cancel(app=principal.app, job_id=job_id)
    )


@router.post("/retrieval-jobs/{job_id}/ack", response_model=RetrievalJobOut)
def acknowledge_retrieval_job(
    job_id: str,
    principal: RetrievalManager,
    container: ContainerDep,
) -> RetrievalJobOut:
    return RetrievalJobOut.model_validate(
        container.retrieval.acknowledge(app=principal.app, job_id=job_id)
    )


@router.head(
    "/retrieval-jobs/{job_id}/objects/{object_id}/content",
    include_in_schema=False,
)
@router.get(
    "/retrieval-jobs/{job_id}/objects/{object_id}/content",
    response_class=StreamingResponse,
)
def get_retrieval_object_content(
    job_id: str,
    object_id: str,
    principal: RetrievalManager,
    container: ContainerDep,
    http_request: Request,
    collection_id: str = Query(),
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    total_bytes, sha256 = container.retrieval.object_content_metadata(
        app=principal.app,
        job_id=job_id,
        collection_id=collection_id,
        object_id=object_id,
    )
    etag = f'"{sha256}"'
    headers = {
        "Content-Length": str(total_bytes),
        "ETag": etag,
        "Content-Type": "application/octet-stream",
    }
    if if_none_match is not None and if_none_match.strip() == etag:
        return Response(status_code=304, headers=headers)
    if http_request.method == "HEAD":
        return Response(headers=headers)
    chunks, returned_bytes, returned_sha256 = container.retrieval.object_content(
        app=principal.app,
        job_id=job_id,
        collection_id=collection_id,
        object_id=object_id,
    )
    if returned_bytes != total_bytes or returned_sha256 != sha256:
        raise RuntimeError("retrieval object content metadata changed")
    return StreamingResponse(
        chunks,
        headers=headers,
        media_type="application/octet-stream",
    )


@router.head("/retrieval-jobs/{job_id}/content", include_in_schema=False)
@router.get(
    "/retrieval-jobs/{job_id}/content",
    response_class=StreamingResponse,
)
def get_retrieval_content(
    job_id: str,
    principal: RetrievalManager,
    container: ContainerDep,
    http_request: Request,
    collection_id: str = Query(),
    path: str = Query(),
    range_header: Annotated[str | None, Header(alias="Range")] = None,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    total_bytes, sha256 = container.retrieval.content_metadata(
        app=principal.app,
        job_id=job_id,
        collection_id=collection_id,
        path=path,
    )
    etag = f'"{sha256}"'
    headers = {
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Content-Type": "application/octet-stream",
    }
    if if_none_match is not None and if_none_match.strip() == etag:
        return Response(status_code=304, headers=headers)
    start, end = _parse_range(range_header, total_bytes)
    status_code = 206 if range_header is not None else 200
    content_length = end - start
    headers["Content-Length"] = str(content_length)
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end - 1}/{total_bytes}"
    if http_request.method == "HEAD":
        return Response(status_code=status_code, headers=headers)
    chunks, returned_bytes, returned_sha256 = container.retrieval.content(
        app=principal.app,
        job_id=job_id,
        collection_id=collection_id,
        path=path,
    )
    if returned_bytes != total_bytes or returned_sha256 != sha256:
        raise RuntimeError("retrieval content metadata changed")
    return StreamingResponse(
        _iter_range(chunks, start=start, size=content_length),
        status_code=status_code,
        headers=headers,
        media_type="application/octet-stream",
    )


def _parse_range(value: str | None, total_bytes: int) -> tuple[int, int]:
    if value is None:
        return 0, total_bytes
    unit, separator, raw = value.partition("=")
    if unit.casefold() != "bytes" or not separator or "," in raw:
        raise BadRequest("only one bytes range is supported")
    start_raw, dash, end_raw = raw.partition("-")
    if not dash:
        raise BadRequest("invalid bytes range")
    try:
        if not start_raw:
            suffix = int(end_raw)
            if suffix <= 0:
                raise ValueError
            start = max(0, total_bytes - suffix)
            end = total_bytes
        else:
            start = int(start_raw)
            end = total_bytes if not end_raw else int(end_raw) + 1
    except ValueError as exc:
        raise BadRequest("invalid bytes range") from exc
    if start < 0 or start >= total_bytes or end <= start or end > total_bytes:
        raise BadRequest("bytes range is outside the file")
    return start, end


def _iter_range(chunks: Iterable[bytes], *, start: int, size: int) -> Iterator[bytes]:
    skip = start
    remaining = size
    for chunk in chunks:
        if skip >= len(chunk):
            skip -= len(chunk)
            continue
        current = chunk[skip : skip + remaining]
        skip = 0
        if current:
            yield current
            remaining -= len(current)
        if remaining == 0:
            return
    if remaining:
        raise RuntimeError("retrieval stream ended before the requested range")

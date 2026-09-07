from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import StreamingResponse
from http_api_contracts import (
    QuotedSha256Identity,
    exact_authority_page_operation,
    mutable_browse_operation,
    operation_interface,
    parse_quoted_sha256_identity,
)
from riverhog_protocol import (
    RETRIEVAL_FILE_BATCH_MAX,
    ArchiveStoreName,
    CollectionIdParameter,
    RetrievalCacheProtection,
    RetrievalCacheSort,
    RetrievalCacheState,
    RetrievalCacheStoreName,
    SortOrder,
)
from riverhog_protocol.errors import BadRequest, InvalidRange, PreconditionFailed

from riverhog_api.auth import CatalogReader, RetrievalManager
from riverhog_api.browse import (
    BrowsePageTokenQuery,
    BrowseQueryParameter,
    canonical_selectors,
    page_payload,
    page_position,
)
from riverhog_api.deps import ContainerDep
from riverhog_api.schemas.retrieval import (
    CreateRetrievalJobRequest,
    RenewRetrievalJobRequest,
    RetrievalCacheObjectListOut,
    RetrievalCacheObjectOut,
    RetrievalCacheStatusOut,
    RetrievalJobOut,
    RetrievalPlanFilePageOut,
    RetrievalPlanOut,
    RetrievalPlanRequest,
)

router = APIRouter(tags=["retrieval"])


def _files(request: RetrievalPlanRequest) -> list[tuple[int, str]]:
    return [(item.collection_id, item.path) for item in request.files]


@router.get("/retrieval-cache", response_model=RetrievalCacheStatusOut)
def retrieval_cache_status(
    principal: CatalogReader,
    container: ContainerDep,
) -> RetrievalCacheStatusOut:
    return RetrievalCacheStatusOut.model_validate(
        container.retrieval.cache_status(principal=principal)
    )


@router.get(
    "/retrieval-cache/objects",
    response_model=RetrievalCacheObjectListOut,
    openapi_extra=mutable_browse_operation(),
)
def list_retrieval_cache_objects(
    principal: CatalogReader,
    container: ContainerDep,
    page_size: int = Query(25, ge=1, le=100),
    page_token: BrowsePageTokenQuery = None,
    q: BrowseQueryParameter = None,
    collection_id: Annotated[CollectionIdParameter | None, Query()] = None,
    source_store: Annotated[ArchiveStoreName | None, Query()] = None,
    cache_store: Annotated[RetrievalCacheStoreName | None, Query()] = None,
    state: Annotated[RetrievalCacheState | None, Query()] = None,
    protection: Annotated[RetrievalCacheProtection | None, Query()] = None,
    expires_before: str | None = Query(None),
    expires_after: str | None = Query(None),
    sort: Annotated[RetrievalCacheSort, Query()] = "cached_at",
    order: Annotated[SortOrder, Query()] = "desc",
) -> RetrievalCacheObjectListOut:
    selectors = canonical_selectors(
        q=q,
        collection_id=collection_id,
        source_store=source_store,
        cache_store=cache_store,
        state=state,
        protection=protection,
        expires_before=expires_before,
        expires_after=expires_after,
        sort=sort,
        order=order,
    )
    position = page_position(
        container,
        principal=principal,
        operation="list_retrieval_cache_objects",
        page_token=page_token,
        selectors=selectors,
    )
    return RetrievalCacheObjectListOut.model_validate(
        page_payload(
            container.retrieval.list_cache_objects(
                page_size=page_size,
                position=position,
                q=q,
                collection_id=collection_id,
                source_store=source_store,
                cache_store=cache_store,
                state=state,
                protection=protection,
                expires_before=expires_before,
                expires_after=expires_after,
                sort=sort,
                order=order,
                principal=principal,
            ),
            container=container,
            principal=principal,
            operation="list_retrieval_cache_objects",
            selectors=selectors,
        )
    )


@router.get(
    "/retrieval-cache/objects/{collection_id}/{source_store}/{object_id}",
    response_model=RetrievalCacheObjectOut,
)
def get_retrieval_cache_object(
    collection_id: CollectionIdParameter,
    source_store: ArchiveStoreName,
    object_id: str,
    principal: CatalogReader,
    container: ContainerDep,
) -> RetrievalCacheObjectOut:
    return RetrievalCacheObjectOut.model_validate(
        container.retrieval.get_cache_object(
            collection_id=collection_id,
            source_store=source_store,
            object_id=object_id,
            principal=principal,
        )
    )


@router.post(
    "/retrieval-plans",
    response_model=RetrievalPlanOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def plan_retrieval(
    request: RetrievalPlanRequest,
    principal: RetrievalManager,
    container: ContainerDep,
) -> RetrievalPlanOut:
    payload = container.retrieval.plan(
        _files(request),
        idempotency_key=request.idempotency_key,
        lease=(
            timedelta(seconds=request.lease_seconds) if request.lease_seconds is not None else None
        ),
        restore_policy=request.restore_policy,
        principal=principal,
    )
    return RetrievalPlanOut.model_validate(payload)


@router.get(
    "/retrieval-plans/{plan_id}",
    response_model=RetrievalPlanOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def get_retrieval_plan(
    plan_id: str,
    principal: RetrievalManager,
    container: ContainerDep,
) -> RetrievalPlanOut:
    return RetrievalPlanOut.model_validate(
        container.retrieval.get_plan(
            app=principal.app,
            key_id=principal.key_id,
            plan_id=plan_id,
        )
    )


@router.post(
    "/retrieval-plans/{plan_id}/advance",
    response_model=RetrievalPlanOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def advance_retrieval_plan(
    plan_id: str,
    principal: RetrievalManager,
    container: ContainerDep,
) -> RetrievalPlanOut:
    return RetrievalPlanOut.model_validate(
        container.retrieval.advance_plan(
            app=principal.app,
            key_id=principal.key_id,
            plan_id=plan_id,
        )
    )


@router.get(
    "/retrieval-plans/{plan_id}/files",
    response_model=RetrievalPlanFilePageOut,
    openapi_extra={
        **operation_interface("client-only-primitive"),
        **exact_authority_page_operation(
            authority="retrieval-plan-files",
            authority_parameter=None,
            cursor_parameter="start_ordinal",
            limit_parameter="page_size",
        ),
    },
)
def list_retrieval_plan_files(
    plan_id: str,
    principal: RetrievalManager,
    container: ContainerDep,
    if_match: Annotated[QuotedSha256Identity, Header(alias="If-Match")],
    start_ordinal: int = Query(0, ge=0, le=RETRIEVAL_FILE_BATCH_MAX),
    page_size: int = Query(100, ge=1, le=100),
) -> RetrievalPlanFilePageOut:
    return RetrievalPlanFilePageOut.model_validate(
        container.retrieval.list_plan_files(
            app=principal.app,
            key_id=principal.key_id,
            plan_id=plan_id,
            etag=parse_quoted_sha256_identity(if_match),
            start_ordinal=start_ordinal,
            page_size=page_size,
        )
    )


@router.post(
    "/retrieval-jobs",
    response_model=RetrievalJobOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def create_retrieval_job(
    request: CreateRetrievalJobRequest,
    principal: RetrievalManager,
    container: ContainerDep,
    if_match: Annotated[QuotedSha256Identity, Header(alias="If-Match")],
) -> RetrievalJobOut:
    plan_etag = parse_quoted_sha256_identity(if_match)
    payload = container.retrieval.create(
        app=principal.app,
        key_id=principal.key_id,
        plan_id=request.plan_id,
        plan_etag=plan_etag,
        event_context=request.event_context,
        principal=principal,
    )
    return RetrievalJobOut.model_validate(payload)


@router.post(
    "/retrieval-jobs/{job_id}/renew",
    response_model=RetrievalJobOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def renew_retrieval_job(
    job_id: str,
    request: RenewRetrievalJobRequest,
    principal: RetrievalManager,
    container: ContainerDep,
) -> RetrievalJobOut:
    return RetrievalJobOut.model_validate(
        container.retrieval.renew(
            app=principal.app,
            key_id=principal.key_id,
            job_id=job_id,
            lease=timedelta(seconds=request.lease_seconds),
        )
    )


@router.get(
    "/retrieval-jobs/{job_id}",
    response_model=RetrievalJobOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def get_retrieval_job(
    job_id: str,
    principal: RetrievalManager,
    container: ContainerDep,
) -> RetrievalJobOut:
    return RetrievalJobOut.model_validate(
        container.retrieval.get(
            app=principal.app,
            key_id=principal.key_id,
            job_id=job_id,
        )
    )


@router.delete(
    "/retrieval-jobs/{job_id}",
    response_model=RetrievalJobOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def cancel_retrieval_job(
    job_id: str,
    principal: RetrievalManager,
    container: ContainerDep,
) -> RetrievalJobOut:
    return RetrievalJobOut.model_validate(
        container.retrieval.cancel(app=principal.app, key_id=principal.key_id, job_id=job_id)
    )


@router.post(
    "/retrieval-jobs/{job_id}/ack",
    response_model=RetrievalJobOut,
    openapi_extra=operation_interface("client-only-primitive"),
)
def acknowledge_retrieval_job(
    job_id: str,
    principal: RetrievalManager,
    container: ContainerDep,
) -> RetrievalJobOut:
    return RetrievalJobOut.model_validate(
        container.retrieval.acknowledge(
            app=principal.app,
            key_id=principal.key_id,
            job_id=job_id,
        )
    )


@router.head(
    "/retrieval-jobs/{job_id}/content",
    include_in_schema=False,
    operation_id="head_retrieval_file",
    openapi_extra=operation_interface("standard-tool/protocol"),
)
@router.get(
    "/retrieval-jobs/{job_id}/content",
    response_class=StreamingResponse,
    openapi_extra=operation_interface("client-only-primitive"),
)
def download_retrieval_file(
    job_id: str,
    principal: RetrievalManager,
    container: ContainerDep,
    http_request: Request,
    collection_id: Annotated[CollectionIdParameter, Query()],
    if_match: Annotated[QuotedSha256Identity, Header(alias="If-Match")],
    path: str = Query(),
    range_header: Annotated[str | None, Header(alias="Range")] = None,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    total_bytes, sha256 = container.retrieval.content_metadata(
        app=principal.app,
        job_id=job_id,
        collection_id=collection_id,
        path=path,
        key_id=principal.key_id,
    )
    etag = f'"{sha256}"'
    if parse_quoted_sha256_identity(if_match) != sha256:
        raise PreconditionFailed("retrieval file identity changed")
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
        offset=start,
        size=content_length,
        key_id=principal.key_id,
    )
    if returned_bytes != total_bytes or returned_sha256 != sha256:
        raise RuntimeError("retrieval content metadata changed")
    return StreamingResponse(
        chunks,
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
    if start < 0 or start >= total_bytes or end <= start:
        raise InvalidRange("bytes range is outside the file")
    return start, min(end, total_bytes)

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from riverhog_api.deps import ContainerDep
from riverhog_api.mappers import map_archive_restore_list, map_fetch, map_fetch_list
from riverhog_api.schemas.fetches import (
    CompleteFetchResponse,
    CreateFetchRequest,
    FetchesResponse,
    FetchFilesResponse,
    FetchManifestResponse,
    FetchStartPlanOut,
    FetchStatusResponse,
    FetchSummaryOut,
    FetchTargetsRequest,
    FetchUploadSessionResponse,
    HotEvictRequest,
    HotEvictResponse,
    StartFetchRequest,
)
from riverhog_api.tus import (
    tus_delete_headers,
    tus_options_headers,
    tus_upload_headers,
    validate_tus_chunk_request,
)
from riverhog_api.urls import public_request_url

router = APIRouter(tags=["fetches"])


def _fetch_status_payload(
    fetch_id: str,
    container: ContainerDep,
    *,
    limit: int,
) -> dict[str, object]:
    payload = container.fetches.status(fetch_id, limit=limit)
    archive_restores = map_archive_restore_list(
        container.archive_restores.list_for_fetch(
            fetch_id,
            page=1,
            per_page=max(1, min(limit, 100)),
            sort="created_at",
            order="desc",
        )
    )
    if limit <= 0:
        archive_restores["restores"] = []
    payload["archive_restores"] = archive_restores
    return payload


@router.post("/hot/evict", response_model=HotEvictResponse)
def evict_hot_targets(
    request: HotEvictRequest,
    container: ContainerDep,
) -> HotEvictResponse:
    payload = container.fetches.evict(request.targets, dry_run=request.dry_run)
    return HotEvictResponse.model_validate(payload)


@router.get("/fetches", response_model=FetchesResponse)
def list_fetches(
    container: ContainerDep,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    state: str | None = Query(None),
    q: str | None = Query(None),
    sort: str = Query("order"),
    order: str = Query("asc"),
) -> FetchesResponse:
    summary = container.fetches.list(
        page=page,
        per_page=per_page,
        state=state,
        q=q,
        sort=sort,
        order=order,
    )
    return FetchesResponse.model_validate(map_fetch_list(summary))


@router.post("/fetches", response_model=FetchSummaryOut)
def create_fetch(
    request: CreateFetchRequest,
    container: ContainerDep,
) -> FetchSummaryOut:
    summary = container.fetches.create(name=request.name, targets=request.targets)
    return FetchSummaryOut.model_validate(map_fetch(summary))


@router.post("/fetches/{fetch_id}/targets", response_model=FetchSummaryOut)
def add_fetch_targets(
    fetch_id: str,
    request: FetchTargetsRequest,
    container: ContainerDep,
) -> FetchSummaryOut:
    summary = container.fetches.add_targets(fetch_id, request.targets)
    return FetchSummaryOut.model_validate(map_fetch(summary))


@router.delete("/fetches/{fetch_id}/targets", response_model=FetchSummaryOut)
def remove_fetch_targets(
    fetch_id: str,
    request: FetchTargetsRequest,
    container: ContainerDep,
) -> FetchSummaryOut:
    summary = container.fetches.remove_targets(fetch_id, request.targets)
    return FetchSummaryOut.model_validate(map_fetch(summary))


@router.post("/fetches/{fetch_id}/start", response_model=FetchSummaryOut | FetchStartPlanOut)
def start_fetch(
    fetch_id: str,
    request: StartFetchRequest,
    container: ContainerDep,
) -> FetchSummaryOut | FetchStartPlanOut:
    if request.dry_run:
        payload = container.fetches.start_plan(fetch_id, archive=request.archive)
        return FetchStartPlanOut.model_validate(payload)
    summary = container.fetches.start(fetch_id, archive=request.archive)
    if request.archive:
        container.archive_restores.create_or_resume_for_fetch(fetch_id)
        summary = container.fetches.get(fetch_id)
    return FetchSummaryOut.model_validate(map_fetch(summary))


@router.get("/fetches/{fetch_id}", response_model=FetchSummaryOut)
def get_fetch(fetch_id: str, container: ContainerDep) -> FetchSummaryOut:
    summary = container.fetches.get(fetch_id)
    return FetchSummaryOut.model_validate(map_fetch(summary))


@router.get("/fetches/{fetch_id}/status", response_model=FetchStatusResponse)
def get_fetch_status(
    fetch_id: str,
    container: ContainerDep,
    limit: Annotated[int, Query(ge=0, le=100)] = 25,
) -> FetchStatusResponse:
    payload = _fetch_status_payload(fetch_id, container, limit=limit)
    return FetchStatusResponse.model_validate(payload)


@router.post("/fetches/{fetch_id}/cancel", response_model=FetchStatusResponse)
def cancel_fetch(
    fetch_id: str,
    container: ContainerDep,
    limit: Annotated[int, Query(ge=0, le=100)] = 25,
) -> FetchStatusResponse:
    summary = container.fetches.get(fetch_id)
    if summary.state.value in {"queued_archive", "restoring_archive"}:
        container.archive_restores.cancel_for_fetch(fetch_id)
    else:
        container.fetches.cancel(fetch_id)
    payload = _fetch_status_payload(fetch_id, container, limit=limit)
    return FetchStatusResponse.model_validate(payload)


@router.get("/fetches/{fetch_id}/files", response_model=FetchFilesResponse)
def list_fetch_files(
    fetch_id: str,
    container: ContainerDep,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    q: str | None = Query(None),
    sort: Literal["target", "collection", "path", "bytes", "hot", "disc"] = Query("target"),
    order: Literal["asc", "desc"] = Query("asc"),
    hot: bool | None = Query(None),
    disc_coverage: bool | None = Query(None),
) -> FetchFilesResponse:
    payload = container.fetches.files(
        fetch_id,
        page=page,
        per_page=per_page,
        q=q,
        sort=sort.casefold(),
        order=order.casefold(),
        hot=hot,
        disc_coverage=disc_coverage,
    )
    return FetchFilesResponse.model_validate(payload)


@router.get("/fetches/{fetch_id}/manifest", response_model=FetchManifestResponse)
def get_manifest(fetch_id: str, container: ContainerDep) -> FetchManifestResponse:
    payload = container.fetches.manifest(fetch_id)
    return FetchManifestResponse.model_validate(payload)


@router.post(
    "/fetches/{fetch_id}/entries/{entry_id}/upload", response_model=FetchUploadSessionResponse
)
def create_or_resume_fetch_entry_upload(
    fetch_id: str,
    entry_id: str,
    request: Request,
    response: Response,
    container: ContainerDep,
) -> FetchUploadSessionResponse:
    payload = container.fetches.create_or_resume_upload(fetch_id=fetch_id, entry_id=entry_id)
    payload["upload_url"] = public_request_url(request)
    response.headers.update(tus_upload_headers(payload, request=request))
    return FetchUploadSessionResponse.model_validate(payload)


@router.patch("/fetches/{fetch_id}/entries/{entry_id}/upload", status_code=204)
async def append_fetch_entry_upload_chunk(
    fetch_id: str,
    entry_id: str,
    request: Request,
    container: ContainerDep,
) -> Response:
    offset, checksum = validate_tus_chunk_request(request)
    payload = await run_in_threadpool(
        container.fetches.append_upload_chunk,
        fetch_id,
        entry_id,
        offset=offset,
        checksum=checksum,
        content=await request.body(),
    )
    return Response(status_code=204, headers=tus_upload_headers(payload, request=request))


@router.head("/fetches/{fetch_id}/entries/{entry_id}/upload", status_code=204)
def head_fetch_entry_upload(
    fetch_id: str,
    entry_id: str,
    request: Request,
    container: ContainerDep,
) -> Response:
    payload = container.fetches.get_entry_upload(fetch_id, entry_id)
    return Response(status_code=204, headers=tus_upload_headers(payload, request=request))


@router.delete("/fetches/{fetch_id}/entries/{entry_id}/upload", status_code=204)
def delete_fetch_entry_upload(
    fetch_id: str,
    entry_id: str,
    container: ContainerDep,
) -> Response:
    container.fetches.cancel_entry_upload(fetch_id, entry_id)
    return Response(status_code=204, headers=tus_delete_headers())


@router.options("/fetches/{fetch_id}/entries/{entry_id}/upload", status_code=204)
def options_fetch_entry_upload() -> Response:
    return Response(status_code=204, headers=tus_options_headers())


@router.post("/fetches/{fetch_id}/complete", response_model=CompleteFetchResponse)
def complete_fetch(fetch_id: str, container: ContainerDep) -> CompleteFetchResponse:
    payload = container.fetches.complete(fetch_id)
    return CompleteFetchResponse.model_validate(payload)

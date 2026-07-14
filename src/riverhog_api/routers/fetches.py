from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query

from riverhog_api.deps import ContainerDep
from riverhog_api.mappers import map_archive_restore_list, map_fetch, map_fetch_list
from riverhog_api.schemas.fetches import (
    CreateFetchRequest,
    FetchesResponse,
    FetchFilesResponse,
    FetchStatusResponse,
    FetchSummaryOut,
    FetchTargetsRequest,
    HotEvictRequest,
    HotEvictResponse,
)

router = APIRouter(tags=["fetches"])


def _fetch_status_payload(fetch_id: str, container: ContainerDep) -> dict[str, object]:
    payload = container.fetches.status(fetch_id)
    payload["archive_restores"] = map_archive_restore_list(
        container.archive_restores.list_for_fetch(
            fetch_id,
            page=1,
            per_page=100,
            sort="created_at",
            order="desc",
        )
    )
    return payload


@router.post("/hot/evict", response_model=HotEvictResponse)
def evict_hot_targets(
    request: HotEvictRequest,
    container: ContainerDep,
) -> HotEvictResponse:
    return HotEvictResponse.model_validate(
        container.fetches.evict(request.targets, dry_run=request.dry_run)
    )


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
    return FetchesResponse.model_validate(
        map_fetch_list(
            container.fetches.list(
                page=page,
                per_page=per_page,
                state=state,
                q=q,
                sort=sort,
                order=order,
            )
        )
    )


@router.post("/fetches", response_model=FetchSummaryOut)
def create_fetch(
    request: CreateFetchRequest,
    container: ContainerDep,
) -> FetchSummaryOut:
    return FetchSummaryOut.model_validate(
        map_fetch(container.fetches.create(name=request.name, targets=request.targets))
    )


@router.post("/fetches/{fetch_id}/targets", response_model=FetchSummaryOut)
def add_fetch_targets(
    fetch_id: str,
    request: FetchTargetsRequest,
    container: ContainerDep,
) -> FetchSummaryOut:
    return FetchSummaryOut.model_validate(
        map_fetch(container.fetches.add_targets(fetch_id, request.targets))
    )


@router.delete("/fetches/{fetch_id}/targets", response_model=FetchSummaryOut)
def remove_fetch_targets(
    fetch_id: str,
    request: FetchTargetsRequest,
    container: ContainerDep,
) -> FetchSummaryOut:
    return FetchSummaryOut.model_validate(
        map_fetch(container.fetches.remove_targets(fetch_id, request.targets))
    )


@router.post("/fetches/{fetch_id}/start", response_model=FetchSummaryOut)
def start_fetch(fetch_id: str, container: ContainerDep) -> FetchSummaryOut:
    summary = container.fetches.start(fetch_id)
    if summary.state.value == "queued_archive":
        container.archive_restores.create_or_resume_for_fetch(fetch_id)
        summary = container.fetches.get(fetch_id)
    return FetchSummaryOut.model_validate(map_fetch(summary))


@router.get("/fetches/{fetch_id}", response_model=FetchSummaryOut)
def get_fetch(fetch_id: str, container: ContainerDep) -> FetchSummaryOut:
    return FetchSummaryOut.model_validate(map_fetch(container.fetches.get(fetch_id)))


@router.get("/fetches/{fetch_id}/status", response_model=FetchStatusResponse)
def get_fetch_status(fetch_id: str, container: ContainerDep) -> FetchStatusResponse:
    return FetchStatusResponse.model_validate(_fetch_status_payload(fetch_id, container))


@router.post("/fetches/{fetch_id}/cancel", response_model=FetchStatusResponse)
def cancel_fetch(fetch_id: str, container: ContainerDep) -> FetchStatusResponse:
    summary = container.fetches.get(fetch_id)
    if summary.state.value in {"queued_archive", "restoring_archive"}:
        container.archive_restores.cancel_for_fetch(fetch_id)
    else:
        container.fetches.cancel(fetch_id)
    return FetchStatusResponse.model_validate(_fetch_status_payload(fetch_id, container))


@router.get("/fetches/{fetch_id}/files", response_model=FetchFilesResponse)
def list_fetch_files(
    fetch_id: str,
    container: ContainerDep,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    q: str | None = Query(None),
    sort: Literal["target", "collection", "path", "bytes", "hot"] = Query("target"),
    order: Literal["asc", "desc"] = Query("asc"),
    hot: bool | None = Query(None),
) -> FetchFilesResponse:
    return FetchFilesResponse.model_validate(
        container.fetches.files(
            fetch_id,
            page=page,
            per_page=per_page,
            q=q,
            sort=sort,
            order=order,
            hot=hot,
        )
    )

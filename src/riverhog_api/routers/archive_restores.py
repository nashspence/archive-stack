from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from riverhog_api.deps import ContainerDep
from riverhog_api.mappers import map_archive_restore, map_archive_restore_list
from riverhog_api.schemas.archive_restores import (
    ArchiveRestoreListOut,
    ArchiveRestoreOut,
)

router = APIRouter(tags=["archive-restores"])


@router.get("/images/{image_id}/disc-rebuild", response_model=ArchiveRestoreOut)
def get_disc_rebuild(
    image_id: str,
    container: ContainerDep,
) -> ArchiveRestoreOut:
    summary = container.archive_restores.get_for_image(image_id)
    return ArchiveRestoreOut.model_validate(map_archive_restore(summary))


@router.get("/archive-restores", response_model=ArchiveRestoreListOut)
def list_archive_restores(
    container: ContainerDep,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    sort: Literal[
        "created_at",
        "id",
        "type",
        "state",
        "ready_at",
        "expires_at",
    ] = Query("created_at"),
    order: Literal["asc", "desc"] = Query("desc"),
    terminal: Literal["active", "terminal", "all"] = Query("all"),
    restore_type: Annotated[
        Literal["fetch_materialization", "disc_rebuild"] | None,
        Query(alias="type"),
    ] = None,
    state: Literal[
        "requested",
        "ready",
        "paused",
        "expired",
        "completed",
        "failed",
        "canceled",
    ]
    | None = Query(None),
    collection: str | None = Query(None),
    image: str | None = Query(None),
) -> ArchiveRestoreListOut:
    summary = container.archive_restores.list(
        page=page,
        per_page=per_page,
        sort=sort,
        order=order,
        terminal=terminal,
        restore_type=restore_type,
        state=state,
        collection=collection,
        image=image,
    )
    return ArchiveRestoreListOut.model_validate(map_archive_restore_list(summary))


@router.get("/archive-restores/{archive_restore_id}", response_model=ArchiveRestoreOut)
def get_archive_restore(
    archive_restore_id: str,
    container: ContainerDep,
) -> ArchiveRestoreOut:
    summary = container.archive_restores.get(archive_restore_id)
    return ArchiveRestoreOut.model_validate(map_archive_restore(summary))


@router.post("/archive-restores/{archive_restore_id}/complete", response_model=ArchiveRestoreOut)
def complete_archive_restore(
    archive_restore_id: str,
    container: ContainerDep,
) -> ArchiveRestoreOut:
    summary = container.archive_restores.complete(archive_restore_id)
    return ArchiveRestoreOut.model_validate(map_archive_restore(summary))


@router.post("/archive-restores/{archive_restore_id}/cancel", response_model=ArchiveRestoreOut)
def cancel_archive_restore(
    archive_restore_id: str,
    container: ContainerDep,
) -> ArchiveRestoreOut:
    summary = container.archive_restores.cancel(archive_restore_id)
    return ArchiveRestoreOut.model_validate(map_archive_restore(summary))


@router.post("/archive-restores/{archive_restore_id}/pause", response_model=ArchiveRestoreOut)
def pause_archive_restore(
    archive_restore_id: str,
    container: ContainerDep,
) -> ArchiveRestoreOut:
    summary = container.archive_restores.pause(archive_restore_id)
    return ArchiveRestoreOut.model_validate(map_archive_restore(summary))


@router.post("/archive-restores/{archive_restore_id}/resume", response_model=ArchiveRestoreOut)
def resume_archive_restore(
    archive_restore_id: str,
    container: ContainerDep,
) -> ArchiveRestoreOut:
    summary = container.archive_restores.resume(archive_restore_id)
    return ArchiveRestoreOut.model_validate(map_archive_restore(summary))


@router.get("/archive-restores/{archive_restore_id}/images/{image_id}/iso")
def get_restored_iso(
    archive_restore_id: str,
    image_id: str,
    container: ContainerDep,
) -> StreamingResponse:
    body = container.archive_restores.iter_restored_iso(archive_restore_id, image_id)
    return StreamingResponse(body, media_type="application/octet-stream")

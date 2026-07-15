from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query

from riverhog_api.deps import ContainerDep
from riverhog_api.mappers import map_archive_restore, map_archive_restore_list
from riverhog_api.schemas.archive_restores import ArchiveRestoreListOut, ArchiveRestoreOut

router = APIRouter(tags=["archive-restores"])


@router.get("/archive-restores", response_model=ArchiveRestoreListOut)
def list_archive_restores(
    container: ContainerDep,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    sort: Literal["created_at", "id", "state", "ready_at", "expires_at"] = Query("created_at"),
    order: Literal["asc", "desc"] = Query("desc"),
    terminal: Literal["active", "terminal", "all"] = Query("all"),
    state: Literal["requested", "ready", "expired", "completed", "failed", "canceled"]
    | None = Query(None),
    collection: str | None = Query(None),
) -> ArchiveRestoreListOut:
    summary = container.archive_restores.list(
        page=page,
        per_page=per_page,
        sort=sort,
        order=order,
        terminal=terminal,
        state=state,
        collection=collection,
    )
    return ArchiveRestoreListOut.model_validate(map_archive_restore_list(summary))


@router.get("/archive-restores/{archive_restore_id}", response_model=ArchiveRestoreOut)
def get_archive_restore(
    archive_restore_id: str,
    container: ContainerDep,
) -> ArchiveRestoreOut:
    return ArchiveRestoreOut.model_validate(
        map_archive_restore(container.archive_restores.get(archive_restore_id))
    )


@router.post("/archive-restores/{archive_restore_id}/cancel", response_model=ArchiveRestoreOut)
def cancel_archive_restore(
    archive_restore_id: str,
    container: ContainerDep,
) -> ArchiveRestoreOut:
    return ArchiveRestoreOut.model_validate(
        map_archive_restore(container.archive_restores.cancel(archive_restore_id))
    )

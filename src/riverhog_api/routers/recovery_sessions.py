from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from riverhog_api.deps import ContainerDep
from riverhog_api.mappers import map_recovery_session, map_recovery_session_list
from riverhog_api.schemas.recovery_sessions import (
    CollectionRestoreStartRequest,
    RecoverySessionListOut,
    RecoverySessionOut,
)

router = APIRouter(tags=["recovery"])


@router.get("/collections/{collection_id:path}/restore-session", response_model=RecoverySessionOut)
def get_collection_restore_session(
    collection_id: str,
    container: ContainerDep,
) -> RecoverySessionOut:
    summary = container.recovery_sessions.get_for_collection(collection_id)
    return RecoverySessionOut.model_validate(map_recovery_session(summary))


@router.post("/collections/{collection_id:path}/restore-session", response_model=RecoverySessionOut)
def create_or_resume_collection_restore_session(
    collection_id: str,
    container: ContainerDep,
    request: CollectionRestoreStartRequest | None = None,
) -> RecoverySessionOut:
    summary = container.recovery_sessions.create_or_resume_for_collection(
        collection_id,
        paths=None if request is None else request.paths,
    )
    return RecoverySessionOut.model_validate(map_recovery_session(summary))


@router.get("/images/{image_id}/rebuild-session", response_model=RecoverySessionOut)
def get_image_rebuild_session(
    image_id: str,
    container: ContainerDep,
) -> RecoverySessionOut:
    summary = container.recovery_sessions.get_for_image(image_id)
    return RecoverySessionOut.model_validate(map_recovery_session(summary))


@router.get("/recovery-sessions", response_model=RecoverySessionListOut)
def list_recovery_sessions(
    container: ContainerDep,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    sort: Literal[
        "created_at",
        "id",
        "type",
        "state",
        "restore_ready_at",
        "restore_expires_at",
    ] = Query("created_at"),
    order: Literal["asc", "desc"] = Query("desc"),
    recovery_type: Annotated[
        Literal["collection_restore", "image_rebuild"] | None,
        Query(alias="type"),
    ] = None,
    state: Literal[
        "restore_requested",
        "ready",
        "expired",
        "completed",
    ]
    | None = Query(None),
    collection: str | None = Query(None),
    image: str | None = Query(None),
) -> RecoverySessionListOut:
    summary = container.recovery_sessions.list(
        page=page,
        per_page=per_page,
        sort=sort,
        order=order,
        recovery_type=recovery_type,
        state=state,
        collection=collection,
        image=image,
    )
    return RecoverySessionListOut.model_validate(map_recovery_session_list(summary))


@router.get("/recovery-sessions/{session_id}", response_model=RecoverySessionOut)
def get_recovery_session(
    session_id: str,
    container: ContainerDep,
) -> RecoverySessionOut:
    summary = container.recovery_sessions.get(session_id)
    return RecoverySessionOut.model_validate(map_recovery_session(summary))


@router.post("/recovery-sessions/{session_id}/complete", response_model=RecoverySessionOut)
def complete_recovery_session(
    session_id: str,
    container: ContainerDep,
) -> RecoverySessionOut:
    summary = container.recovery_sessions.complete(session_id)
    return RecoverySessionOut.model_validate(map_recovery_session(summary))


@router.get("/recovery-sessions/{session_id}/images/{image_id}/iso")
def get_recovered_iso(
    session_id: str,
    image_id: str,
    container: ContainerDep,
) -> StreamingResponse:
    body = container.recovery_sessions.iter_restored_iso(session_id, image_id)
    return StreamingResponse(body, media_type="application/octet-stream")

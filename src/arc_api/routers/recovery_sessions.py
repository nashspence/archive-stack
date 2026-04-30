from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from arc_api.deps import ContainerDep
from arc_api.mappers import map_recovery_session
from arc_api.schemas.recovery_sessions import (
    CollectionRestoreMaterializeRequest,
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
) -> RecoverySessionOut:
    summary = container.recovery_sessions.create_or_resume_for_collection(collection_id)
    return RecoverySessionOut.model_validate(map_recovery_session(summary))


@router.post("/images/{image_id}/rebuild-session", response_model=RecoverySessionOut)
def create_or_resume_image_rebuild_session(
    image_id: str,
    container: ContainerDep,
) -> RecoverySessionOut:
    summary = container.recovery_sessions.create_or_resume_for_image(image_id)
    return RecoverySessionOut.model_validate(map_recovery_session(summary))


@router.get("/images/{image_id}/rebuild-session", response_model=RecoverySessionOut)
def get_image_rebuild_session(
    image_id: str,
    container: ContainerDep,
) -> RecoverySessionOut:
    summary = container.recovery_sessions.get_for_image(image_id)
    return RecoverySessionOut.model_validate(map_recovery_session(summary))


@router.get("/recovery-sessions/{session_id}", response_model=RecoverySessionOut)
def get_recovery_session(
    session_id: str,
    container: ContainerDep,
) -> RecoverySessionOut:
    summary = container.recovery_sessions.get(session_id)
    return RecoverySessionOut.model_validate(map_recovery_session(summary))


@router.post("/recovery-sessions/{session_id}/approve", response_model=RecoverySessionOut)
def approve_recovery_session(
    session_id: str,
    container: ContainerDep,
) -> RecoverySessionOut:
    summary = container.recovery_sessions.approve(session_id)
    return RecoverySessionOut.model_validate(map_recovery_session(summary))


@router.post("/recovery-sessions/{session_id}/complete", response_model=RecoverySessionOut)
def complete_recovery_session(
    session_id: str,
    container: ContainerDep,
) -> RecoverySessionOut:
    summary = container.recovery_sessions.complete(session_id)
    return RecoverySessionOut.model_validate(map_recovery_session(summary))


@router.post(
    "/recovery-sessions/{session_id}/collections/{collection_id:path}/materialize",
    response_model=RecoverySessionOut,
)
def materialize_collection_restore_files(
    session_id: str,
    collection_id: str,
    request: CollectionRestoreMaterializeRequest,
    container: ContainerDep,
) -> RecoverySessionOut:
    summary = container.recovery_sessions.materialize_collection_files(
        session_id,
        collection_id,
        paths=request.paths,
    )
    return RecoverySessionOut.model_validate(map_recovery_session(summary))


@router.get("/recovery-sessions/{session_id}/images/{image_id}/iso")
def get_recovered_iso(
    session_id: str,
    image_id: str,
    container: ContainerDep,
) -> StreamingResponse:
    body = container.recovery_sessions.iter_restored_iso(session_id, image_id)
    return StreamingResponse(body, media_type="application/octet-stream")

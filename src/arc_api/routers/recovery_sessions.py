from __future__ import annotations

from fastapi import APIRouter

from arc_api.deps import ContainerDep
from arc_api.mappers import map_recovery_session
from arc_api.schemas.recovery_sessions import RecoverySessionOut

router = APIRouter(tags=["recovery"])


@router.post("/images/{image_id}/recovery-session", response_model=RecoverySessionOut)
def create_or_resume_recovery_session(
    image_id: str,
    container: ContainerDep,
) -> RecoverySessionOut:
    summary = container.recovery_sessions.create_or_resume_for_image(image_id)
    return RecoverySessionOut.model_validate(map_recovery_session(summary))


@router.get("/images/{image_id}/recovery-session", response_model=RecoverySessionOut)
def get_recovery_session_for_image(
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

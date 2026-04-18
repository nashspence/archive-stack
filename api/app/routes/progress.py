from __future__ import annotations

from fastapi import APIRouter

from ..progress import activation_session_stream_name, download_stream_name, collection_stream_name, progress_stream, upload_stream_name

router = APIRouter(prefix="/v1/progress", tags=["progress"])


@router.get("/uploads/{upload_id}/stream")
def upload_progress_stream(upload_id: str):
    return progress_stream(upload_stream_name(upload_id))


@router.get("/collections/{collection_id}/stream")
def collection_progress_stream(collection_id: str):
    return progress_stream(collection_stream_name(collection_id))


@router.get("/activation-sessions/{session_id}/stream")
def activation_session_progress_stream(session_id: str):
    return progress_stream(activation_session_stream_name(session_id))


@router.get("/downloads/{session_id}/stream")
def download_progress_stream(session_id: str):
    return progress_stream(download_stream_name(session_id))

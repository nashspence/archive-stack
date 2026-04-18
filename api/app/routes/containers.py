from __future__ import annotations

import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..config import DOWNLOAD_CHUNK_SIZE, TUSD_BASE_URL
from ..crypto import AgeEncryptionError, decrypt_tree
from ..db import SessionLocal
from ..iso import create_iso_from_container_root
from ..models import (
    ArchivePiece,
    ActivationSession,
    Container,
    ContainerEntry,
    ContainerFinalizationWebhookSubscription,
    DownloadSession,
    UploadSlot,
)
from ..notifications import (
    backfill_container_finalization_notifications_for_subscription,
    complete_container_finalization_notifications,
    isoformat_z,
)
from ..planner import force_close_pending, import_closed_containers
from ..progress import download_stream_name, publish_progress
from ..schemas import (
    BurnConfirmResponse,
    ActivationSessionCompleteResponse,
    ActivationSessionCreateResponse,
    ActivationUploadSlotRequest,
    ContainerFinalizationWebhookCreateRequest,
    ContainerFinalizationWebhookCreateResponse,
    DownloadSessionCreateResponse,
    IsoCreateRequest,
    IsoCreateResponse,
    IsoRegisterRequest,
    InactiveError,
    TreeNode,
    TreeResponse,
    UploadSlotCreateResponse,
)
from ..storage import active_container_root, activation_staging_root, canonical_tree_hash, container_tree_nodes, maybe_release_collection_buffer_after_archive, normalize_relpath, container_root, rebuild_collection_export, registered_iso_storage_path

router = APIRouter(prefix="/v1/containers", tags=["containers"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


Db = Annotated[Session, Depends(get_db)]


@router.post("/finalization-webhooks", response_model=ContainerFinalizationWebhookCreateResponse)
def create_container_finalization_webhook_subscription(
    body: ContainerFinalizationWebhookCreateRequest,
    db: Db,
) -> ContainerFinalizationWebhookCreateResponse:
    subscription = ContainerFinalizationWebhookSubscription(
        webhook_url=str(body.webhook_url),
        reminder_interval_seconds=body.reminder_interval_seconds,
    )
    db.add(subscription)
    db.flush()
    pending_container_count = backfill_container_finalization_notifications_for_subscription(db, subscription.id)
    db.commit()
    return ContainerFinalizationWebhookCreateResponse(
        subscription_id=subscription.id,
        webhook_url=subscription.webhook_url,
        reminder_interval_seconds=subscription.reminder_interval_seconds,
        pending_container_count=pending_container_count,
    )


@router.post("/flush")
def flush_pending(force: bool = False, db: Session = Depends(get_db)):
    closed = force_close_pending(db) if force else []
    container_ids = import_closed_containers(db, closed) if closed else []
    touched_collections: set[str] = set()
    for container_id in container_ids:
        container = db.execute(select(Container).where(Container.id == container_id).options(selectinload(Container.archive_pieces).selectinload(ArchivePiece.collection_file))).scalar_one()
        touched_collections.update(piece.collection_file.collection_id for piece in container.archive_pieces)
    for collection_id in sorted(touched_collections):
        rebuild_collection_export(db, collection_id)
    return {"status": "ok", "closed_containers": container_ids}


@router.get("/{container_id}/tree", response_model=TreeResponse)
def container_tree(container_id: str, db: Db) -> TreeResponse:
    container = db.execute(select(Container).where(Container.id == container_id).options(selectinload(Container.entries))).scalar_one_or_none()
    if container is None:
        raise HTTPException(status_code=404, detail="container not found")
    nodes = [TreeNode(**node) for node in container_tree_nodes(container)]
    return TreeResponse(root_id=container_id, root_kind="container", nodes=nodes)


@router.get("/{container_id}/content/{container_relative_path:path}", responses={409: {"model": InactiveError}})
def get_container_file(container_id: str, container_relative_path: str, db: Db):
    container_rel = normalize_relpath(container_relative_path)
    container = db.execute(select(Container).where(Container.id == container_id)).scalar_one_or_none()
    if container is None:
        raise HTTPException(status_code=404, detail="container not found")
    entry = db.execute(select(ContainerEntry).where(ContainerEntry.container_id == container_id, ContainerEntry.relative_path == container_rel)).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status_code=404, detail="file not found")
    if not container.active_root_abs_path:
        return JSONResponse(status_code=409, content={"error": "container_inactive", "message": f"This file is on container {container_id}, which is inactive right now.", "container_ids": [container_id]})
    path = Path(container.active_root_abs_path) / container_rel
    if not path.exists():
        return JSONResponse(status_code=409, content={"error": "container_inactive", "message": f"This file is on container {container_id}, which is inactive right now.", "container_ids": [container_id]})
    return FileResponse(path=path, filename=Path(container_rel).name, media_type="application/octet-stream")


@router.post("/{container_id}/activation/sessions", response_model=ActivationSessionCreateResponse)
def create_activation_session(container_id: str, db: Db) -> ActivationSessionCreateResponse:
    container = db.execute(select(Container).where(Container.id == container_id).options(selectinload(Container.entries))).scalar_one_or_none()
    if container is None:
        raise HTTPException(status_code=404, detail="container not found")
    session = ActivationSession(container_id=container_id, expected_total_bytes=container.total_root_bytes)
    db.add(session)
    db.commit()
    return ActivationSessionCreateResponse(session_id=session.id, container_id=container_id, expected_total_bytes=container.total_root_bytes, expected_files=len(container.entries), progress_stream_url=f"/v1/progress/activation-sessions/{session.id}/stream")


@router.get("/{container_id}/activation/sessions/{session_id}/expected")
def activation_session_expected(container_id: str, session_id: str, db: Db):
    session = db.execute(select(ActivationSession).where(ActivationSession.id == session_id, ActivationSession.container_id == container_id)).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="activation session not found")
    entries = db.execute(select(ContainerEntry).where(ContainerEntry.container_id == container_id).order_by(ContainerEntry.relative_path)).scalars().all()
    return {
        "container_id": container_id,
        "session_id": session_id,
        "entries": [
            {
                "relative_path": e.relative_path,
                "size_bytes": e.stored_size_bytes or e.size_bytes,
                "sha256": e.stored_sha256 or e.sha256,
                "kind": e.kind,
                "logical_size_bytes": e.size_bytes,
                "logical_sha256": e.sha256,
            }
            for e in entries
        ],
    }


@router.post("/{container_id}/activation/sessions/{session_id}/uploads", response_model=UploadSlotCreateResponse)
def create_activation_upload_slot(container_id: str, session_id: str, body: ActivationUploadSlotRequest, db: Db) -> UploadSlotCreateResponse:
    session_obj = db.execute(select(ActivationSession).where(ActivationSession.id == session_id, ActivationSession.container_id == container_id)).scalar_one_or_none()
    if session_obj is None:
        raise HTTPException(status_code=404, detail="activation session not found")
    if session_obj.status not in {"open", "uploading"}:
        raise HTTPException(status_code=409, detail="activation session is closed")
    rel = normalize_relpath(body.relative_path)
    entry = db.execute(select(ContainerEntry).where(ContainerEntry.container_id == container_id, ContainerEntry.relative_path == rel)).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status_code=409, detail="path is not part of the known container root")
    existing = db.execute(select(UploadSlot).where(UploadSlot.activation_session_id == session_id, UploadSlot.relative_path == rel)).scalar_one_or_none()
    if existing and existing.status == "completed":
        raise HTTPException(status_code=409, detail="path already uploaded for this activation session")

    upload_id = secrets.token_hex(16)
    upload_token = secrets.token_urlsafe(32)
    slot = UploadSlot(
        upload_id=upload_id,
        upload_token=upload_token,
        kind="activation_file",
        relative_path=rel,
        size_bytes=entry.stored_size_bytes or entry.size_bytes,
        expected_sha256=(entry.stored_sha256 or entry.sha256),
        activation_session_id=session_id,
    )
    db.add(slot)
    session_obj.status = "uploading"
    db.commit()
    return UploadSlotCreateResponse(upload_id=upload_id, upload_token=upload_token, tus_create_url=TUSD_BASE_URL, tus_metadata={"upload_id": upload_id, "upload_token": upload_token, "relative_path": rel}, upload_stream_url=f"/v1/progress/uploads/{upload_id}/stream", aggregate_stream_url=f"/v1/progress/activation-sessions/{session_id}/stream")


@router.post("/{container_id}/activation/sessions/{session_id}/complete", response_model=ActivationSessionCompleteResponse)
def complete_activation_session(container_id: str, session_id: str, db: Db) -> ActivationSessionCompleteResponse:
    container = db.execute(select(Container).where(Container.id == container_id).options(selectinload(Container.entries), selectinload(Container.archive_pieces).selectinload(ArchivePiece.collection_file))).scalar_one_or_none()
    if container is None:
        raise HTTPException(status_code=404, detail="container not found")
    activation_session = db.execute(select(ActivationSession).where(ActivationSession.id == session_id, ActivationSession.container_id == container_id)).scalar_one_or_none()
    if activation_session is None:
        raise HTTPException(status_code=404, detail="activation session not found")

    staging = activation_staging_root(session_id)
    if not staging.exists():
        raise HTTPException(status_code=409, detail="activation session has no uploaded root")

    actual_hash, total_bytes, rows = canonical_tree_hash(staging)
    expected = {
        (
            e.relative_path,
            int(e.stored_size_bytes or e.size_bytes),
            str(e.stored_sha256 or e.sha256),
        )
        for e in container.entries
    }
    actual = {(str(r["relative_path"]), int(r["size_bytes"]), str(r["sha256"])) for r in rows}
    if actual_hash != container.contents_hash or actual != expected or total_bytes != container.total_root_bytes:
        activation_session.status = "failed"
        db.commit()
        raise HTTPException(status_code=409, detail="uploaded root does not match the known container contents")

    active = active_container_root(container_id)
    decrypted = active.parent / f".{container_id}.decrypting"
    try:
        decrypt_tree(staging, decrypted)
    except AgeEncryptionError as exc:
        activation_session.status = "failed"
        db.commit()
        if decrypted.exists():
            shutil.rmtree(decrypted, ignore_errors=True)
        raise HTTPException(status_code=409, detail=f"uploaded root could not be decrypted: {exc}") from exc

    _logical_hash, _logical_total_bytes, logical_rows = canonical_tree_hash(decrypted)
    expected_logical = {(e.relative_path, e.size_bytes, e.sha256) for e in container.entries}
    actual_logical = {(str(r["relative_path"]), int(r["size_bytes"]), str(r["sha256"])) for r in logical_rows}
    if actual_logical != expected_logical:
        activation_session.status = "failed"
        db.commit()
        shutil.rmtree(decrypted, ignore_errors=True)
        raise HTTPException(status_code=409, detail="decrypted root does not match the cataloged logical container contents")

    if active.exists():
        shutil.rmtree(active)
    active.parent.mkdir(parents=True, exist_ok=True)
    decrypted.replace(active)
    shutil.rmtree(staging, ignore_errors=True)
    container.active_root_abs_path = str(active)
    container.status = "active"
    activation_session.status = "completed"
    activation_session.uploaded_bytes = container.total_root_bytes
    db.commit()

    touched_collections = sorted({piece.collection_file.collection_id for piece in container.archive_pieces})
    for collection_id in touched_collections:
        rebuild_collection_export(db, collection_id)
    return ActivationSessionCompleteResponse(container_id=container_id, session_id=session_id, status="active", contents_hash=actual_hash)


@router.delete("/{container_id}/activation")
def deactivate_container(container_id: str, db: Db):
    container = db.execute(select(Container).where(Container.id == container_id).options(selectinload(Container.archive_pieces).selectinload(ArchivePiece.collection_file))).scalar_one_or_none()
    if container is None:
        raise HTTPException(status_code=404, detail="container not found")
    active = active_container_root(container_id)
    if active.exists():
        shutil.rmtree(active)
    container.active_root_abs_path = None
    container.status = "inactive"
    db.commit()
    touched_collections = sorted({piece.collection_file.collection_id for piece in container.archive_pieces})
    for collection_id in touched_collections:
        rebuild_collection_export(db, collection_id)
    return {"status": "ok", "container_id": container_id}


@router.post("/{container_id}/iso/register")
def register_iso(container_id: str, body: IsoRegisterRequest, db: Db):
    container = db.get(Container, container_id)
    if container is None:
        raise HTTPException(status_code=404, detail="container not found")
    src = Path(body.server_path)
    if not src.exists() or not src.is_file():
        raise HTTPException(status_code=404, detail="server_path not found")
    dest = registered_iso_storage_path(container_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    container.iso_abs_path = str(dest)
    container.iso_size_bytes = dest.stat().st_size
    container.burn_confirmed_at = None
    db.commit()
    return {"status": "ok", "container_id": container_id, "iso_path": str(dest), "size_bytes": container.iso_size_bytes}


@router.post("/{container_id}/iso/create", response_model=IsoCreateResponse)
def author_iso(container_id: str, body: IsoCreateRequest, db: Db) -> IsoCreateResponse:
    container = db.get(Container, container_id)
    if container is None:
        raise HTTPException(status_code=404, detail="container not found")
    output = registered_iso_storage_path(container_id)
    if output.exists() and not body.overwrite:
        raise HTTPException(status_code=409, detail="iso already exists; pass overwrite=true to replace it")
    try:
        created = create_iso_from_container_root(
            container_id,
            container_root(container_id),
            requested_label=body.volume_label,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    container.iso_abs_path = str(created)
    container.iso_size_bytes = created.stat().st_size
    container.burn_confirmed_at = None
    db.commit()
    return IsoCreateResponse(
        container_id=container_id,
        iso_path=str(created),
        size_bytes=container.iso_size_bytes,
    )


@router.get("/{container_id}/iso/content")
def download_registered_iso(container_id: str, db: Db):
    container = db.get(Container, container_id)
    if container is None:
        raise HTTPException(status_code=404, detail="container not found")
    if not container.iso_abs_path or not Path(container.iso_abs_path).exists():
        raise HTTPException(status_code=409, detail="iso not registered or not active")
    iso_path = Path(container.iso_abs_path)
    return FileResponse(
        path=str(iso_path),
        filename=iso_path.name,
        media_type="application/octet-stream",
    )


@router.post("/{container_id}/burn/confirm", response_model=BurnConfirmResponse)
def confirm_burn(container_id: str, db: Db) -> BurnConfirmResponse:
    container = db.execute(
        select(Container)
        .where(Container.id == container_id)
        .options(selectinload(Container.archive_pieces).selectinload(ArchivePiece.collection_file))
    ).scalar_one_or_none()
    if container is None:
        raise HTTPException(status_code=404, detail="container not found")
    if not container.iso_abs_path or not Path(container.iso_abs_path).exists():
        raise HTTPException(status_code=409, detail="iso not registered or not active")

    if container.burn_confirmed_at is None:
        container.burn_confirmed_at = datetime.now(timezone.utc)
        complete_container_finalization_notifications(db, container_id)
        db.commit()

    released_collection_ids: list[str] = []
    touched_collections = sorted({piece.collection_file.collection_id for piece in container.archive_pieces})
    for collection_id in touched_collections:
        if maybe_release_collection_buffer_after_archive(db, collection_id):
            released_collection_ids.append(collection_id)

    return BurnConfirmResponse(
        container_id=container_id,
        burn_confirmed_at=isoformat_z(container.burn_confirmed_at) or "",
        released_collection_ids=released_collection_ids,
    )


@router.post("/{container_id}/download-sessions", response_model=DownloadSessionCreateResponse)
async def create_download_session(container_id: str, db: Db) -> DownloadSessionCreateResponse:
    container = db.get(Container, container_id)
    if container is None:
        raise HTTPException(status_code=404, detail="container not found")
    if not container.iso_abs_path or not Path(container.iso_abs_path).exists():
        raise HTTPException(status_code=409, detail="iso not registered or not active")
    total_bytes = Path(container.iso_abs_path).stat().st_size
    session = DownloadSession(container_id=container_id, total_bytes=total_bytes)
    db.add(session)
    db.commit()
    await publish_progress(download_stream_name(session.id), {"status": "ready", "bytes_sent": 0, "total_bytes": total_bytes})
    return DownloadSessionCreateResponse(session_id=session.id, container_id=container_id, total_bytes=total_bytes, progress_stream_url=f"/v1/progress/downloads/{session.id}/stream", content_url=f"/v1/containers/downloads/{session.id}/content")


@router.get("/downloads/{session_id}/content")
async def stream_iso(session_id: str, db: Db):
    session = db.get(DownloadSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="download session not found")
    container = db.get(Container, session.container_id)
    if container is None or not container.iso_abs_path or not Path(container.iso_abs_path).exists():
        raise HTTPException(status_code=409, detail="iso is inactive")
    iso_path = Path(container.iso_abs_path)
    total_bytes = iso_path.stat().st_size

    async def iterator():
        sent = 0
        worker_db = SessionLocal()
        try:
            dl = worker_db.get(DownloadSession, session_id)
            if dl is not None:
                dl.status = "streaming"
                worker_db.commit()
            await publish_progress(download_stream_name(session_id), {"status": "streaming", "bytes_sent": 0, "total_bytes": total_bytes})
            with iso_path.open("rb") as handle:
                while True:
                    chunk = handle.read(DOWNLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    sent += len(chunk)
                    dl = worker_db.get(DownloadSession, session_id)
                    if dl is not None:
                        dl.bytes_sent = sent
                        dl.status = "streaming"
                        worker_db.commit()
                    await publish_progress(download_stream_name(session_id), {"status": "streaming", "bytes_sent": sent, "total_bytes": total_bytes})
                    yield chunk
            dl = worker_db.get(DownloadSession, session_id)
            if dl is not None:
                dl.bytes_sent = sent
                dl.status = "completed"
                worker_db.commit()
            await publish_progress(download_stream_name(session_id), {"status": "completed", "bytes_sent": sent, "total_bytes": total_bytes})
        except Exception:
            dl = worker_db.get(DownloadSession, session_id)
            if dl is not None:
                dl.bytes_sent = sent
                dl.status = "failed"
                worker_db.commit()
            await publish_progress(download_stream_name(session_id), {"status": "failed", "bytes_sent": sent, "total_bytes": total_bytes})
            raise
        finally:
            worker_db.close()

    headers = {"Content-Length": str(total_bytes), "Content-Disposition": f'attachment; filename="{iso_path.name}"'}
    return StreamingResponse(iterator(), media_type="application/octet-stream", headers=headers)

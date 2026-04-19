from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..config import CONTAINER_CFG, CONTAINER_ROOTS_DIR, CONTAINER_STATE_DIR, ensure_managed_directory
from ..crypto import AgeEncryptionError, decrypt_tree
from ..db import SessionLocal
from ..iso import create_iso_from_container_root
from ..models import ArchivePiece, ActivationSession, Container, ContainerEntry
from ..notifications import complete_container_finalization_notifications, isoformat_z
from ..planner import force_close_pending, import_closed_containers, load_state, pick, preview_container
from ..schemas import (
    ActivationSessionCompleteResponse,
    ActivationSessionCreateResponse,
    BurnConfirmResponse,
    ContainerListResponse,
    ContainerSummary,
    IsoCreateRequest,
    IsoCreateResponse,
    IsoRegisterRequest,
    PartitioningPoolStatusResponse,
    TreeNode,
    TreeResponse,
)
from ..storage import (
    active_container_root,
    activation_staging_root,
    canonical_tree_hash,
    container_root,
    container_tree_nodes,
    maybe_release_collection_buffer_after_archive,
    rebuild_collection_export,
    registered_iso_storage_path,
)

router = APIRouter(prefix="/v1/containers", tags=["containers"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


Db = Annotated[Session, Depends(get_db)]


def _pool_status_message(state: str, *, fill_bytes: int, spill_fill_bytes: int) -> str:
    if state == "empty":
        return "The partitioning pool is empty."
    if state == "ready":
        return (
            "The partitioning pool can close another container now. "
            f"Collections with priority splits close at {spill_fill_bytes} bytes; others close at {fill_bytes} bytes."
        )
    if state == "overflow":
        return "The partitioning pool is above its buffer cap and needs a forced close to keep moving."
    return "The partitioning pool is waiting for more sealed collection data before it can close another container."


def _partitioning_pool_status() -> PartitioningPoolStatusResponse:
    state = load_state(CONTAINER_STATE_DIR, CONTAINER_CFG)
    if state["cfg"] != CONTAINER_CFG:
        raise RuntimeError("container state config mismatch")

    pending_items = state["items"]
    pending_bytes = sum(int(item["bytes"]) for item in pending_items)
    ready_candidate = pick(
        pending_items,
        state["collections"],
        state["cfg"]["target"],
        state["cfg"]["fill"],
        state["cfg"]["spill_fill"],
        False,
    )
    overflow_candidate: list[dict] = []
    force_close_required = False
    if not ready_candidate and pending_items and pending_bytes > state["cfg"]["buffer_max"]:
        overflow_candidate = pick(
            pending_items,
            state["collections"],
            state["cfg"]["target"],
            state["cfg"]["fill"],
            state["cfg"]["spill_fill"],
            True,
        )
        force_close_required = bool(overflow_candidate)

    candidate = ready_candidate or overflow_candidate
    preview = None
    if candidate:
        preview = preview_container(
            CONTAINER_STATE_DIR,
            state,
            candidate,
            CONTAINER_ROOTS_DIR,
        )

    if not pending_items:
        status = "empty"
    elif ready_candidate:
        status = "ready"
    elif force_close_required:
        status = "overflow"
    else:
        status = "waiting"

    return PartitioningPoolStatusResponse(
        state=status,
        status_message=_pool_status_message(
            status,
            fill_bytes=state["cfg"]["fill"],
            spill_fill_bytes=state["cfg"]["spill_fill"],
        ),
        pending_collection_count=len(state["collections"]),
        pending_piece_group_count=len(pending_items),
        pending_bytes=pending_bytes,
        target_bytes=state["cfg"]["target"],
        fill_bytes=state["cfg"]["fill"],
        spill_fill_bytes=state["cfg"]["spill_fill"],
        buffer_max_bytes=state["cfg"]["buffer_max"],
        force_close_required=force_close_required,
        closeable_now=bool(ready_candidate),
        next_container_id=preview["name"] if preview else None,
        next_container_bytes=preview["used"] if preview else None,
        next_container_free_bytes=preview["free"] if preview else None,
        next_container_collection_count=len(preview["collections"]) if preview else None,
        next_container_piece_group_count=len(preview["items"]) if preview else None,
    )


@router.get("", response_model=ContainerListResponse)
def list_containers(db: Db) -> ContainerListResponse:
    containers = (
        db.execute(
            select(Container)
            .options(selectinload(Container.entries))
            .order_by(Container.created_at.desc(), Container.id.asc())
        )
        .scalars()
        .all()
    )
    return ContainerListResponse(
        containers=[
            ContainerSummary(
                container_id=container.id,
                status=container.status,
                description=container.description,
                total_root_bytes=container.total_root_bytes,
                contents_hash=container.contents_hash,
                entry_count=len(container.entries),
                active_root_present=bool(container.active_root_abs_path),
                iso_present=bool(container.iso_abs_path and Path(container.iso_abs_path).exists()),
                iso_size_bytes=container.iso_size_bytes,
                root_path=container.root_abs_path,
                active_root_path=container.active_root_abs_path,
                iso_path=container.iso_abs_path,
                burn_confirmed_at=isoformat_z(container.burn_confirmed_at),
                created_at=isoformat_z(container.created_at) or "",
            )
            for container in containers
        ]
    )


@router.get("/pool", response_model=PartitioningPoolStatusResponse)
def partitioning_pool_status() -> PartitioningPoolStatusResponse:
    return _partitioning_pool_status()


@router.post("/flush")
def flush_pending(force: bool = False, db: Session = Depends(get_db)):
    closed = force_close_pending(db) if force else []
    container_ids = import_closed_containers(db, closed) if closed else []
    touched_collections: set[str] = set()
    for container_id in container_ids:
        container = (
            db.execute(
                select(Container)
                .where(Container.id == container_id)
                .options(selectinload(Container.archive_pieces).selectinload(ArchivePiece.collection_file))
            )
            .scalar_one()
        )
        touched_collections.update(piece.collection_file.collection_id for piece in container.archive_pieces)
    for collection_id in sorted(touched_collections):
        rebuild_collection_export(db, collection_id)
    return {"status": "ok", "closed_containers": container_ids}


@router.get("/{container_id}/tree", response_model=TreeResponse)
def container_tree(container_id: str, db: Db) -> TreeResponse:
    container = (
        db.execute(select(Container).where(Container.id == container_id).options(selectinload(Container.entries)))
        .scalar_one_or_none()
    )
    if container is None:
        raise HTTPException(status_code=404, detail="container not found")
    nodes = [TreeNode(**node) for node in container_tree_nodes(container)]
    return TreeResponse(root_id=container_id, root_kind="container", nodes=nodes)


@router.post("/{container_id}/activation/sessions", response_model=ActivationSessionCreateResponse)
def create_activation_session(container_id: str, db: Db) -> ActivationSessionCreateResponse:
    container = (
        db.execute(select(Container).where(Container.id == container_id).options(selectinload(Container.entries)))
        .scalar_one_or_none()
    )
    if container is None:
        raise HTTPException(status_code=404, detail="container not found")
    session = ActivationSession(container_id=container_id, expected_total_bytes=container.total_root_bytes)
    db.add(session)
    db.flush()
    staging_root = activation_staging_root(session.id)
    if staging_root.exists():
        shutil.rmtree(staging_root, ignore_errors=True)
    ensure_managed_directory(staging_root)
    db.commit()
    return ActivationSessionCreateResponse(
        session_id=session.id,
        container_id=container_id,
        expected_total_bytes=container.total_root_bytes,
        expected_files=len(container.entries),
        staging_path=str(staging_root),
    )


@router.get("/{container_id}/activation/sessions/{session_id}/expected")
def activation_session_expected(container_id: str, session_id: str, db: Db):
    session = (
        db.execute(
            select(ActivationSession).where(
                ActivationSession.id == session_id,
                ActivationSession.container_id == container_id,
            )
        )
        .scalar_one_or_none()
    )
    if session is None:
        raise HTTPException(status_code=404, detail="activation session not found")
    entries = (
        db.execute(
            select(ContainerEntry)
            .where(ContainerEntry.container_id == container_id)
            .order_by(ContainerEntry.relative_path)
        )
        .scalars()
        .all()
    )
    return {
        "container_id": container_id,
        "session_id": session_id,
        "staging_path": str(activation_staging_root(session_id)),
        "entries": [
            {
                "relative_path": entry.relative_path,
                "size_bytes": entry.stored_size_bytes or entry.size_bytes,
                "sha256": entry.stored_sha256 or entry.sha256,
                "kind": entry.kind,
                "logical_size_bytes": entry.size_bytes,
                "logical_sha256": entry.sha256,
            }
            for entry in entries
        ],
    }


@router.post("/{container_id}/activation/sessions/{session_id}/complete", response_model=ActivationSessionCompleteResponse)
def complete_activation_session(container_id: str, session_id: str, db: Db) -> ActivationSessionCompleteResponse:
    container = (
        db.execute(
            select(Container)
            .where(Container.id == container_id)
            .options(
                selectinload(Container.entries),
                selectinload(Container.archive_pieces).selectinload(ArchivePiece.collection_file),
            )
        )
        .scalar_one_or_none()
    )
    if container is None:
        raise HTTPException(status_code=404, detail="container not found")
    activation_session = (
        db.execute(
            select(ActivationSession).where(
                ActivationSession.id == session_id,
                ActivationSession.container_id == container_id,
            )
        )
        .scalar_one_or_none()
    )
    if activation_session is None:
        raise HTTPException(status_code=404, detail="activation session not found")
    if activation_session.status != "open":
        raise HTTPException(status_code=409, detail="activation session is closed")

    staging = activation_staging_root(session_id)
    if not staging.exists():
        raise HTTPException(status_code=409, detail="activation session staging directory is missing")

    actual_hash, total_bytes, rows = canonical_tree_hash(staging)
    expected = {
        (
            entry.relative_path,
            int(entry.stored_size_bytes or entry.size_bytes),
            str(entry.stored_sha256 or entry.sha256),
        )
        for entry in container.entries
    }
    actual = {(str(row["relative_path"]), int(row["size_bytes"]), str(row["sha256"])) for row in rows}
    if actual_hash != container.contents_hash or actual != expected or total_bytes != container.total_root_bytes:
        activation_session.status = "failed"
        db.commit()
        raise HTTPException(status_code=409, detail="staged root does not match the known container contents")

    active = active_container_root(container_id)
    decrypted = active.parent / f".{container_id}.decrypting"
    try:
        decrypt_tree(staging, decrypted)
    except AgeEncryptionError as exc:
        activation_session.status = "failed"
        db.commit()
        if decrypted.exists():
            shutil.rmtree(decrypted, ignore_errors=True)
        raise HTTPException(status_code=409, detail=f"staged root could not be decrypted: {exc}") from exc

    _logical_hash, _logical_total_bytes, logical_rows = canonical_tree_hash(decrypted)
    expected_logical = {(entry.relative_path, entry.size_bytes, entry.sha256) for entry in container.entries}
    actual_logical = {
        (str(row["relative_path"]), int(row["size_bytes"]), str(row["sha256"]))
        for row in logical_rows
    }
    if actual_logical != expected_logical:
        activation_session.status = "failed"
        db.commit()
        shutil.rmtree(decrypted, ignore_errors=True)
        raise HTTPException(status_code=409, detail="decrypted root does not match the cataloged logical container contents")

    if active.exists():
        shutil.rmtree(active)
    ensure_managed_directory(active.parent)
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
    return ActivationSessionCompleteResponse(
        container_id=container_id,
        session_id=session_id,
        status="active",
        contents_hash=actual_hash,
    )


@router.delete("/{container_id}/activation")
def deactivate_container(container_id: str, db: Db):
    container = (
        db.execute(
            select(Container)
            .where(Container.id == container_id)
            .options(selectinload(Container.archive_pieces).selectinload(ArchivePiece.collection_file))
        )
        .scalar_one_or_none()
    )
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
    ensure_managed_directory(dest.parent)
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


@router.api_route("/{container_id}/iso/content", methods=["GET", "HEAD"])
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
    container = (
        db.execute(
            select(Container)
            .where(Container.id == container_id)
            .options(selectinload(Container.archive_pieces).selectinload(ArchivePiece.collection_file))
        )
        .scalar_one_or_none()
    )
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

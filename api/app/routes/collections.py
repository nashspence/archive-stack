from __future__ import annotations

import secrets
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..config import TUSD_BASE_URL
from ..db import SessionLocal
from ..models import ArchivePiece, Collection, CollectionDirectory, CollectionFile, UploadSlot
from ..planner import import_closed_containers, ingest_collection
from ..schemas import (
    CollectionCreateRequest,
    CollectionCreateResponse,
    CollectionDirectoryCreateRequest,
    InactiveError,
    SealCollectionResponse,
    TreeNode,
    TreeResponse,
    UploadSlotCreateRequest,
    UploadSlotCreateResponse,
)
from ..storage import (
    inactive_collection_hash_bundle_path,
    normalize_relpath,
    normalize_root_node_name,
    path_parents,
    rebuild_collection_export,
    recompute_collection_file_runtime,
    refresh_collection_hash_artifacts,
    release_collection_buffer_files,
)

router = APIRouter(prefix="/v1/collections", tags=["collections"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


Db = Annotated[Session, Depends(get_db)]


@router.post("", response_model=CollectionCreateResponse)
def create_collection(body: CollectionCreateRequest, db: Db) -> CollectionCreateResponse:
    collection_id = normalize_root_node_name(body.root_node_name)
    if db.get(Collection, collection_id) is not None:
        raise HTTPException(status_code=409, detail="root node name already exists")
    db.add(
        Collection(
            id=collection_id,
            description=body.description,
            keep_buffer_after_archive=body.keep_buffer_after_archive,
        )
    )
    db.commit()
    return CollectionCreateResponse(
        collection_id=collection_id,
        status="open",
        keep_buffer_after_archive=body.keep_buffer_after_archive,
    )


@router.post("/{collection_id}/directories")
def create_directory(collection_id: str, body: CollectionDirectoryCreateRequest, db: Db):
    collection = db.get(Collection, collection_id)
    if collection is None:
        raise HTTPException(status_code=404, detail="collection not found")
    if collection.status != "open":
        raise HTTPException(status_code=409, detail="collection is sealed")
    rel = normalize_relpath(body.relative_path)
    exists = db.scalar(select(CollectionDirectory).where(CollectionDirectory.collection_id == collection_id, CollectionDirectory.relative_path == rel))
    if exists is None:
        db.add(CollectionDirectory(collection_id=collection_id, relative_path=rel))
        db.commit()
        rebuild_collection_export(db, collection_id)
    return {"status": "ok", "collection_id": collection_id, "relative_path": rel}


@router.post("/{collection_id}/uploads", response_model=UploadSlotCreateResponse)
def create_upload_slot(collection_id: str, body: UploadSlotCreateRequest, db: Db) -> UploadSlotCreateResponse:
    collection = db.get(Collection, collection_id)
    if collection is None:
        raise HTTPException(status_code=404, detail="collection not found")
    if collection.status != "open":
        raise HTTPException(status_code=409, detail="collection is sealed")
    relative_path = normalize_relpath(body.relative_path)
    existing = db.scalar(select(CollectionFile).where(CollectionFile.collection_id == collection_id, CollectionFile.relative_path == relative_path))
    if existing is not None:
        raise HTTPException(status_code=409, detail="file path already exists for this collection")

    if len(body.mode) != 4 or body.mode[0] != "0" or any(char not in "01234567" for char in body.mode[1:]):
        raise HTTPException(status_code=400, detail="mode must be a zero-prefixed octal string like 0644")
    if body.sha256 and any(char not in string.hexdigits for char in body.sha256):
        raise HTTPException(status_code=400, detail="sha256 must be exactly 64 hexadecimal characters")
    try:
        parsed_mtime = datetime.fromisoformat(body.mtime.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="mtime must be an RFC3339 UTC timestamp") from exc
    if parsed_mtime.tzinfo is None or parsed_mtime.utcoffset() != timezone.utc.utcoffset(parsed_mtime):
        raise HTTPException(status_code=400, detail="mtime must be an RFC3339 UTC timestamp")

    collection_file = CollectionFile(
        collection_id=collection_id,
        relative_path=relative_path,
        size_bytes=body.size_bytes,
        expected_sha256=body.sha256.lower() if body.sha256 else None,
        mode=body.mode,
        mtime=body.mtime,
        uid=body.uid,
        gid=body.gid,
    )
    db.add(collection_file)
    db.flush()

    upload_id = secrets.token_hex(16)
    upload_token = secrets.token_urlsafe(32)
    slot = UploadSlot(
        upload_id=upload_id,
        upload_token=upload_token,
        kind="collection_file",
        relative_path=relative_path,
        size_bytes=body.size_bytes,
        expected_sha256=body.sha256.lower() if body.sha256 else None,
        collection_file_id=collection_file.id,
    )
    db.add(slot)
    for parent in path_parents(relative_path):
        if db.scalar(select(CollectionDirectory).where(CollectionDirectory.collection_id == collection_id, CollectionDirectory.relative_path == parent)) is None:
            db.add(CollectionDirectory(collection_id=collection_id, relative_path=parent))
    db.commit()

    return UploadSlotCreateResponse(
        upload_id=upload_id,
        upload_token=upload_token,
        tus_create_url=TUSD_BASE_URL,
        tus_metadata={"upload_id": upload_id, "upload_token": upload_token, "relative_path": relative_path},
        upload_stream_url=f"/v1/progress/uploads/{upload_id}/stream",
        aggregate_stream_url=f"/v1/progress/collections/{collection_id}/stream",
    )


@router.post("/{collection_id}/seal", response_model=SealCollectionResponse)
def seal_collection(collection_id: str, db: Db) -> SealCollectionResponse:
    collection = (
        db.execute(select(Collection).where(Collection.id == collection_id).options(selectinload(Collection.files).selectinload(CollectionFile.uploads), selectinload(Collection.directories)))
        .scalar_one_or_none()
    )
    if collection is None:
        raise HTTPException(status_code=404, detail="collection not found")
    if collection.status != "open":
        raise HTTPException(status_code=409, detail="collection already sealed")

    for jf in collection.files:
        if jf.buffer_abs_path is None or not Path(jf.buffer_abs_path).exists():
            raise HTTPException(status_code=409, detail=f"collection file {jf.relative_path} is not fully uploaded")
    refresh_collection_hash_artifacts(db, collection_id)
    result = ingest_collection(db, collection_id)
    closed_ids = import_closed_containers(db, result["closed"])
    rebuild_collection_export(db, collection_id)
    return SealCollectionResponse(collection_id=collection_id, status="sealed", closed_containers=closed_ids, buffer_bytes=result["buffer_bytes"])


@router.get("/{collection_id}/tree", response_model=TreeResponse)
def collection_tree(collection_id: str, db: Db) -> TreeResponse:
    collection = (
        db.execute(
            select(Collection)
            .where(Collection.id == collection_id)
            .options(selectinload(Collection.directories), selectinload(Collection.files).selectinload(CollectionFile.archive_pieces).selectinload(ArchivePiece.container))
        )
        .scalar_one_or_none()
    )
    if collection is None:
        raise HTTPException(status_code=404, detail="collection not found")

    nodes: list[TreeNode] = []
    explicit_dirs = {d.relative_path for d in collection.directories}
    derived_dirs = set()
    for jf in collection.files:
        for parent in path_parents(jf.relative_path):
            derived_dirs.add(parent)
    for rel in sorted(explicit_dirs | derived_dirs):
        nodes.append(TreeNode(path=rel, kind="directory", active=True, source="virtual", status="listed"))
    for jf in sorted(collection.files, key=lambda x: x.relative_path):
        active_path, source, container_ids = recompute_collection_file_runtime(jf)
        nodes.append(
            TreeNode(
                path=jf.relative_path,
                kind="file",
                size_bytes=jf.size_bytes,
                active=active_path is not None,
                source=source,
                container_ids=container_ids or sorted({p.container_id for p in jf.archive_pieces}),
                status=jf.status,
                extra={"error": jf.error_message} if jf.error_message else None,
            )
        )
    db.commit()
    return TreeResponse(root_id=collection_id, root_kind="collection", nodes=nodes)


@router.get("/{collection_id}/content/{relative_path:path}", responses={409: {"model": InactiveError}})
def get_collection_file(collection_id: str, relative_path: str, db: Db):
    rel = normalize_relpath(relative_path)
    collection_file = (
        db.execute(
            select(CollectionFile)
            .where(CollectionFile.collection_id == collection_id, CollectionFile.relative_path == rel)
            .options(selectinload(CollectionFile.archive_pieces).selectinload(ArchivePiece.container))
        )
        .scalar_one_or_none()
    )
    if collection_file is None:
        raise HTTPException(status_code=404, detail="file not found")
    active_path, _source, container_ids = recompute_collection_file_runtime(collection_file)
    db.commit()
    if active_path is None:
        message = collection_file.error_message or "This file is not active right now."
        return JSONResponse(status_code=409, content={"error": "inactive_on_container", "message": message, "container_ids": container_ids})
    return FileResponse(path=str(active_path), filename=Path(rel).name, media_type="application/octet-stream")


@router.post("/{collection_id}/buffer/release")
def release_collection_buffer(collection_id: str, db: Db):
    if db.get(Collection, collection_id) is None:
        raise HTTPException(status_code=404, detail="collection not found")
    release_collection_buffer_files(db, collection_id)
    return {"status": "ok", "collection_id": collection_id}


@router.get("/{collection_id}/hash-manifest-proof")
def download_collection_hash_manifest_bundle(collection_id: str, db: Db):
    if db.get(Collection, collection_id) is None:
        raise HTTPException(status_code=404, detail="collection not found")
    bundle_path = inactive_collection_hash_bundle_path(collection_id)
    if not bundle_path.exists():
        raise HTTPException(status_code=404, detail="collection hash manifest bundle not found")
    return FileResponse(
        path=str(bundle_path),
        filename=f"{collection_id}-hash-manifest-proof.zip",
        media_type="application/zip",
    )

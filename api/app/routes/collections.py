from __future__ import annotations

import shutil
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..config import ACTIVE_BUFFER_ROOT, ensure_managed_directory
from ..db import SessionLocal
from ..models import ArchivePiece, Collection, CollectionFile
from ..notifications import isoformat_z
from ..planner import import_closed_containers, ingest_collection
from ..schemas import (
    CollectionCreateRequest,
    CollectionCreateResponse,
    CollectionListResponse,
    CollectionSummary,
    SealCollectionResponse,
    TreeNode,
    TreeResponse,
)
from ..storage import (
    collection_intake_root,
    collection_live_counts,
    collection_tree_nodes_from_root,
    export_collection_root,
    inactive_collection_hash_manifest_path,
    inactive_collection_hash_proof_path,
    normalize_root_node_name,
    rebuild_collection_export,
    recompute_collection_file_runtime,
    refresh_collection_hash_artifacts,
    release_collection_buffer_files,
    sync_collection_from_buffer,
)

router = APIRouter(prefix="/v1/collections", tags=["collections"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


Db = Annotated[Session, Depends(get_db)]


def _open_collection_counts(collection_id: str) -> tuple[int, int]:
    try:
        return collection_live_counts(collection_id)
    except ValueError:
        return 0, 0


@router.get("", response_model=CollectionListResponse)
def list_collections(db: Db) -> CollectionListResponse:
    collections = (
        db.execute(
            select(Collection)
            .options(selectinload(Collection.files), selectinload(Collection.directories))
            .order_by(Collection.created_at.desc(), Collection.id.asc())
        )
        .scalars()
        .all()
    )

    summaries: list[CollectionSummary] = []
    for collection in collections:
        intake_path = str(collection_intake_root(collection.id)) if collection.status == "open" else None
        hash_manifest_path = None
        hash_proof_path = None
        if collection.status != "open":
            hash_manifest_path = str(inactive_collection_hash_manifest_path(collection.id))
            hash_proof_path = str(inactive_collection_hash_proof_path(collection.id))
        if collection.status == "open":
            file_count, directory_count = _open_collection_counts(collection.id)
        else:
            file_count, directory_count = len(collection.files), len(collection.directories)
        summaries.append(
            CollectionSummary(
                collection_id=collection.id,
                status=collection.status,
                description=collection.description,
                keep_buffer_after_archive=collection.keep_buffer_after_archive,
                file_count=file_count,
                directory_count=directory_count,
                created_at=isoformat_z(collection.created_at) or "",
                sealed_at=isoformat_z(collection.sealed_at),
                intake_path=intake_path,
                export_path=str(export_collection_root(collection.id)),
                hash_manifest_path=hash_manifest_path,
                hash_proof_path=hash_proof_path,
            )
        )
    return CollectionListResponse(collections=summaries)


@router.post("", response_model=CollectionCreateResponse)
def create_collection(body: CollectionCreateRequest, db: Db) -> CollectionCreateResponse:
    collection_id = normalize_root_node_name(body.root_node_name)
    intake_root = collection_intake_root(collection_id)
    active_root = ACTIVE_BUFFER_ROOT / collection_id

    if db.get(Collection, collection_id) is not None or intake_root.exists() or active_root.exists():
        raise HTTPException(status_code=409, detail="collection name already exists")

    ensure_managed_directory(intake_root)
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
        intake_path=str(intake_root),
    )


@router.post("/{collection_id}/seal", response_model=SealCollectionResponse)
def seal_collection(collection_id: str, db: Db) -> SealCollectionResponse:
    collection = (
        db.execute(
            select(Collection)
            .where(Collection.id == collection_id)
            .options(selectinload(Collection.files), selectinload(Collection.directories))
        )
        .scalar_one_or_none()
    )
    if collection is None:
        raise HTTPException(status_code=404, detail="collection not found")
    if collection.status != "open":
        raise HTTPException(status_code=409, detail="collection already sealed")

    intake_root = collection_intake_root(collection_id)
    if not intake_root.exists() or not intake_root.is_dir():
        raise HTTPException(status_code=409, detail="collection intake directory is missing")

    claimed_root = ACTIVE_BUFFER_ROOT / collection_id
    if claimed_root.exists():
        raise HTTPException(status_code=409, detail="collection buffer already exists")

    claimed_root.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(intake_root), str(claimed_root))
        sync_collection_from_buffer(db, collection_id)
        refresh_collection_hash_artifacts(db, collection_id)
        result = ingest_collection(db, collection_id)
        closed_ids = import_closed_containers(db, result["closed"])
        rebuild_collection_export(db, collection_id)
    except Exception:
        db.rollback()
        if claimed_root.exists() and not intake_root.exists():
            intake_root.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(claimed_root), str(intake_root))
        raise

    return SealCollectionResponse(
        collection_id=collection_id,
        status="sealed",
        closed_containers=closed_ids,
        buffer_bytes=result["buffer_bytes"],
    )


@router.get("/{collection_id}/tree", response_model=TreeResponse)
def collection_tree(collection_id: str, db: Db) -> TreeResponse:
    collection = (
        db.execute(
            select(Collection)
            .where(Collection.id == collection_id)
            .options(
                selectinload(Collection.directories),
                selectinload(Collection.files)
                .selectinload(CollectionFile.archive_pieces)
                .selectinload(ArchivePiece.container),
            )
        )
        .scalar_one_or_none()
    )
    if collection is None:
        raise HTTPException(status_code=404, detail="collection not found")

    if collection.status == "open":
        nodes = [TreeNode(**node) for node in collection_tree_nodes_from_root(collection_intake_root(collection_id), source="intake", status="open")]
        return TreeResponse(root_id=collection_id, root_kind="collection", nodes=nodes)

    nodes: list[TreeNode] = []
    explicit_dirs = {d.relative_path for d in collection.directories}
    derived_dirs = set()
    for collection_file in collection.files:
        parts = collection_file.relative_path.split("/")
        for index in range(1, len(parts)):
            derived_dirs.add("/".join(parts[:index]))
    for rel in sorted(explicit_dirs | derived_dirs):
        nodes.append(TreeNode(path=rel, kind="directory", active=True, source="virtual", status="listed"))
    for collection_file in sorted(collection.files, key=lambda item: item.relative_path):
        active_path, source, container_ids = recompute_collection_file_runtime(collection_file)
        nodes.append(
            TreeNode(
                path=collection_file.relative_path,
                kind="file",
                size_bytes=collection_file.size_bytes,
                active=active_path is not None,
                source=source,
                container_ids=container_ids or sorted({piece.container_id for piece in collection_file.archive_pieces}),
                status=collection_file.status,
                extra=None,
            )
        )
    return TreeResponse(root_id=collection_id, root_kind="collection", nodes=nodes)


@router.post("/{collection_id}/buffer/release")
def release_collection_buffer(collection_id: str, db: Db):
    if db.get(Collection, collection_id) is None:
        raise HTTPException(status_code=404, detail="collection not found")
    release_collection_buffer_files(db, collection_id)
    return {"status": "ok", "collection_id": collection_id}

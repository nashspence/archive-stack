from __future__ import annotations

import string
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..config import TUSD_BASE_URL
from ..db import SessionLocal
from ..models import ArchivePiece, Job, JobDirectory, JobFile, UploadSlot
from ..planner import import_closed_discs, ingest_job
from ..schemas import (
    JobCreateRequest,
    JobCreateResponse,
    JobDirectoryCreateRequest,
    OfflineError,
    SealJobResponse,
    TreeNode,
    TreeResponse,
    UploadSlotCreateRequest,
    UploadSlotCreateResponse,
)
from ..storage import (
    cold_job_hash_bundle_path,
    normalize_relpath,
    normalize_root_node_name,
    path_parents,
    rebuild_job_export,
    recompute_job_file_runtime,
    refresh_job_hash_artifacts,
    release_job_buffer_files,
)

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


Db = Annotated[Session, Depends(get_db)]


@router.post("", response_model=JobCreateResponse)
def create_job(body: JobCreateRequest, db: Db) -> JobCreateResponse:
    job_id = normalize_root_node_name(body.root_node_name)
    if db.get(Job, job_id) is not None:
        raise HTTPException(status_code=409, detail="root node name already exists")
    db.add(
        Job(
            id=job_id,
            description=body.description,
            keep_buffer_after_archive=body.keep_buffer_after_archive,
        )
    )
    db.commit()
    return JobCreateResponse(
        job_id=job_id,
        status="open",
        keep_buffer_after_archive=body.keep_buffer_after_archive,
    )


@router.post("/{job_id}/directories")
def create_directory(job_id: str, body: JobDirectoryCreateRequest, db: Db):
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != "open":
        raise HTTPException(status_code=409, detail="job is sealed")
    rel = normalize_relpath(body.relative_path)
    exists = db.scalar(select(JobDirectory).where(JobDirectory.job_id == job_id, JobDirectory.relative_path == rel))
    if exists is None:
        db.add(JobDirectory(job_id=job_id, relative_path=rel))
        db.commit()
        rebuild_job_export(db, job_id)
    return {"status": "ok", "job_id": job_id, "relative_path": rel}


@router.post("/{job_id}/uploads", response_model=UploadSlotCreateResponse)
def create_upload_slot(job_id: str, body: UploadSlotCreateRequest, db: Db) -> UploadSlotCreateResponse:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != "open":
        raise HTTPException(status_code=409, detail="job is sealed")
    relative_path = normalize_relpath(body.relative_path)
    existing = db.scalar(select(JobFile).where(JobFile.job_id == job_id, JobFile.relative_path == relative_path))
    if existing is not None:
        raise HTTPException(status_code=409, detail="file path already exists for this job")

    if len(body.mode) != 4 or body.mode[0] != "0" or any(char not in "01234567" for char in body.mode[1:]):
        raise HTTPException(status_code=400, detail="mode must be a zero-prefixed octal string like 0644")
    if body.sha256 and any(char not in string.hexdigits for char in body.sha256):
        raise HTTPException(status_code=400, detail="sha256 must be exactly 64 hexadecimal characters")
    try:
        datetime.fromisoformat(body.mtime.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="mtime must be an RFC3339 UTC timestamp") from exc

    job_file = JobFile(
        job_id=job_id,
        relative_path=relative_path,
        size_bytes=body.size_bytes,
        expected_sha256=body.sha256.lower() if body.sha256 else None,
        mode=body.mode,
        mtime=body.mtime,
        uid=body.uid,
        gid=body.gid,
    )
    db.add(job_file)
    db.flush()

    upload_id = secrets.token_hex(16)
    upload_token = secrets.token_urlsafe(32)
    slot = UploadSlot(
        upload_id=upload_id,
        upload_token=upload_token,
        kind="job_file",
        relative_path=relative_path,
        size_bytes=body.size_bytes,
        expected_sha256=body.sha256.lower() if body.sha256 else None,
        job_file_id=job_file.id,
    )
    db.add(slot)
    for parent in path_parents(relative_path):
        if db.scalar(select(JobDirectory).where(JobDirectory.job_id == job_id, JobDirectory.relative_path == parent)) is None:
            db.add(JobDirectory(job_id=job_id, relative_path=parent))
    db.commit()

    return UploadSlotCreateResponse(
        upload_id=upload_id,
        upload_token=upload_token,
        tus_create_url=TUSD_BASE_URL,
        tus_metadata={"upload_id": upload_id, "upload_token": upload_token, "relative_path": relative_path},
        upload_stream_url=f"/v1/progress/uploads/{upload_id}/stream",
        aggregate_stream_url=f"/v1/progress/jobs/{job_id}/stream",
    )


@router.post("/{job_id}/seal", response_model=SealJobResponse)
def seal_job(job_id: str, db: Db) -> SealJobResponse:
    job = (
        db.execute(select(Job).where(Job.id == job_id).options(selectinload(Job.files).selectinload(JobFile.uploads), selectinload(Job.directories)))
        .scalar_one_or_none()
    )
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != "open":
        raise HTTPException(status_code=409, detail="job already sealed")

    for jf in job.files:
        if jf.buffer_abs_path is None or not Path(jf.buffer_abs_path).exists():
            raise HTTPException(status_code=409, detail=f"job file {jf.relative_path} is not fully uploaded")
    refresh_job_hash_artifacts(db, job_id)
    result = ingest_job(db, job_id)
    closed_ids = import_closed_discs(db, result["closed"])
    rebuild_job_export(db, job_id)
    return SealJobResponse(job_id=job_id, status="sealed", closed_discs=closed_ids, buffer_bytes=result["buffer_bytes"])


@router.get("/{job_id}/tree", response_model=TreeResponse)
def job_tree(job_id: str, db: Db) -> TreeResponse:
    job = (
        db.execute(
            select(Job)
            .where(Job.id == job_id)
            .options(selectinload(Job.directories), selectinload(Job.files).selectinload(JobFile.archive_pieces).selectinload(ArchivePiece.disc))
        )
        .scalar_one_or_none()
    )
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

    nodes: list[TreeNode] = []
    explicit_dirs = {d.relative_path for d in job.directories}
    derived_dirs = set()
    for jf in job.files:
        for parent in path_parents(jf.relative_path):
            derived_dirs.add(parent)
    for rel in sorted(explicit_dirs | derived_dirs):
        nodes.append(TreeNode(path=rel, kind="directory", online=True, source="virtual", status="listed"))
    for jf in sorted(job.files, key=lambda x: x.relative_path):
        online_path, source, disc_ids = recompute_job_file_runtime(jf)
        nodes.append(
            TreeNode(
                path=jf.relative_path,
                kind="file",
                size_bytes=jf.size_bytes,
                online=online_path is not None,
                source=source,
                disc_ids=disc_ids or sorted({p.disc_id for p in jf.archive_pieces}),
                status=jf.status,
                extra={"error": jf.error_message} if jf.error_message else None,
            )
        )
    db.commit()
    return TreeResponse(root_id=job_id, root_kind="job", nodes=nodes)


@router.get("/{job_id}/content/{relative_path:path}", responses={409: {"model": OfflineError}})
def get_job_file(job_id: str, relative_path: str, db: Db):
    rel = normalize_relpath(relative_path)
    job_file = (
        db.execute(
            select(JobFile)
            .where(JobFile.job_id == job_id, JobFile.relative_path == rel)
            .options(selectinload(JobFile.archive_pieces).selectinload(ArchivePiece.disc))
        )
        .scalar_one_or_none()
    )
    if job_file is None:
        raise HTTPException(status_code=404, detail="file not found")
    online_path, _source, disc_ids = recompute_job_file_runtime(job_file)
    db.commit()
    if online_path is None:
        message = job_file.error_message or "This file is not online right now."
        return JSONResponse(status_code=409, content={"error": "offline_on_disc", "message": message, "disc_ids": disc_ids})
    return FileResponse(path=str(online_path), filename=Path(rel).name, media_type="application/octet-stream")


@router.post("/{job_id}/buffer/release")
def release_job_buffer(job_id: str, db: Db):
    if db.get(Job, job_id) is None:
        raise HTTPException(status_code=404, detail="job not found")
    release_job_buffer_files(db, job_id)
    return {"status": "ok", "job_id": job_id}


@router.get("/{job_id}/hash-manifest-proof")
def download_job_hash_manifest_bundle(job_id: str, db: Db):
    if db.get(Job, job_id) is None:
        raise HTTPException(status_code=404, detail="job not found")
    bundle_path = cold_job_hash_bundle_path(job_id)
    if not bundle_path.exists():
        raise HTTPException(status_code=404, detail="job hash manifest bundle not found")
    return FileResponse(
        path=str(bundle_path),
        filename=f"{job_id}-hash-manifest-proof.zip",
        media_type="application/zip",
    )

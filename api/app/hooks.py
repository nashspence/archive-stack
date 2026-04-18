from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .auth import hook_auth_ok
from .config import INCOMING_DIR
from .db import SessionLocal
from .models import UploadSlot, JobFile, CacheSession
from .progress import cache_session_stream_name, job_stream_name, publish_progress, upload_stream_name
from .storage import aggregate_cache_progress, aggregate_job_progress, cache_staging_file_path, file_sha256, job_buffer_path, normalize_relpath, rebuild_job_export, refresh_job_hash_artifacts

router = APIRouter(prefix="/internal", tags=["internal"])


def _decode_metadata_value(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _error(status_code: int, message: str, reject: bool = False, stop: bool = False):
    body = {
        "HTTPResponse": {
            "StatusCode": status_code,
            "Body": json.dumps({"message": message}),
            "Header": {"Content-Type": "application/json"},
        }
    }
    if reject:
        body["RejectUpload"] = True
    if stop:
        body["StopUpload"] = True
    return JSONResponse(body)


async def _publish_aggregate(db, slot: UploadSlot) -> None:
    if slot.job_file_id:
        current, total = aggregate_job_progress(db, slot.job_file.job_id)
        await publish_progress(job_stream_name(slot.job_file.job_id), {"status": slot.status, "bytes_current": current, "bytes_total": total})
    elif slot.cache_session_id:
        current, total = aggregate_cache_progress(db, slot.cache_session_id)
        await publish_progress(cache_session_stream_name(slot.cache_session_id), {"status": slot.status, "bytes_current": current, "bytes_total": total})


@router.post("/tusd-hooks")
async def tusd_hooks(
    request: Request,
    hook_name: str | None = Header(default=None, alias="Hook-Name"),
    hook_secret: str = "",
):
    if not hook_auth_ok(hook_secret):
        raise HTTPException(status_code=403, detail="forbidden")

    payload = await request.json()
    metadata = payload.get("MetaData") or {}
    upload_id = _decode_metadata_value(metadata.get("upload_id")) or payload.get("ID")
    upload_token = _decode_metadata_value(metadata.get("upload_token"))
    relative_path = _decode_metadata_value(metadata.get("relative_path"))

    db = SessionLocal()
    try:
        if hook_name == "pre-create":
            if not upload_id or not upload_token or not relative_path:
                return _error(400, "missing upload metadata", reject=True)
            slot = (
                db.execute(
                    select(UploadSlot)
                    .where(UploadSlot.upload_id == upload_id)
                    .options(selectinload(UploadSlot.job_file), selectinload(UploadSlot.cache_session))
                )
                .scalar_one_or_none()
            )
            if slot is None:
                return _error(404, "unknown upload slot", reject=True)
            if not secrets.compare_digest(slot.upload_token, upload_token):
                return _error(403, "invalid upload token", reject=True)
            if slot.relative_path != normalize_relpath(relative_path):
                return _error(409, "upload path does not match reserved slot", reject=True)
            if int(payload.get("Size") or 0) != int(slot.size_bytes):
                return _error(409, "upload size does not match reserved slot", reject=True)

            slot.status = "uploading"
            slot.current_offset = 0
            db.commit()
            incoming_path = INCOMING_DIR / f"{upload_id}.bin"
            incoming_path.parent.mkdir(parents=True, exist_ok=True)
            return JSONResponse({"ChangeFileInfo": {"ID": upload_id, "Storage": {"Path": str(incoming_path)}}})

        if not upload_id:
            return JSONResponse({})

        slot = (
            db.execute(
                select(UploadSlot)
                .where(UploadSlot.upload_id == upload_id)
                .options(selectinload(UploadSlot.job_file).selectinload(JobFile.archive_pieces), selectinload(UploadSlot.cache_session))
            )
            .scalar_one_or_none()
        )
        if slot is None:
            return JSONResponse({})

        if hook_name == "post-create":
            await publish_progress(upload_stream_name(upload_id), {"status": "created", "offset": 0, "size": slot.size_bytes})
            await _publish_aggregate(db, slot)
            return JSONResponse({})

        if hook_name == "post-receive":
            slot.current_offset = int(payload.get("Offset") or 0)
            slot.status = "uploading"
            db.commit()
            await publish_progress(upload_stream_name(upload_id), {"status": "uploading", "offset": slot.current_offset, "size": slot.size_bytes})
            await _publish_aggregate(db, slot)
            return JSONResponse({})

        if hook_name == "post-finish":
            incoming_path = INCOMING_DIR / f"{upload_id}.bin"
            if not incoming_path.exists():
                slot.status = "failed"
                slot.error_message = "tusd finished but incoming file was missing"
                db.commit()
                await publish_progress(upload_stream_name(upload_id), {"status": "failed", "error": slot.error_message})
                return JSONResponse({})

            actual_sha256 = file_sha256(incoming_path)
            if slot.expected_sha256 and actual_sha256 != slot.expected_sha256.lower():
                slot.status = "failed"
                slot.error_message = "sha256 mismatch"
                db.commit()
                await publish_progress(upload_stream_name(upload_id), {"status": "failed", "error": slot.error_message})
                return JSONResponse({})

            if slot.kind == "job_file":
                job_file = slot.job_file
                assert job_file is not None
                final_path = job_buffer_path(job_file.job_id, job_file.relative_path)
                final_path.parent.mkdir(parents=True, exist_ok=True)
                incoming_path.replace(final_path)
                slot.final_abs_path = str(final_path)
                job_file.actual_sha256 = actual_sha256
                job_file.buffer_abs_path = str(final_path)
                job_file.status = "online"
                job_file.error_message = None
                rebuild_job_export(db, job_file.job_id)
                refresh_job_hash_artifacts(db, job_file.job_id)
            else:
                final_path = cache_staging_file_path(slot.cache_session_id, slot.relative_path)
                final_path.parent.mkdir(parents=True, exist_ok=True)
                incoming_path.replace(final_path)
                slot.final_abs_path = str(final_path)
                cache_session = slot.cache_session
                assert cache_session is not None
                cache_session.status = "uploading"

            slot.actual_sha256 = actual_sha256
            slot.current_offset = slot.size_bytes
            slot.status = "completed"
            slot.error_message = None
            db.commit()
            await publish_progress(upload_stream_name(upload_id), {"status": "completed", "offset": slot.size_bytes, "size": slot.size_bytes, "sha256": actual_sha256})
            await _publish_aggregate(db, slot)
            return JSONResponse({})

        if hook_name == "post-terminate":
            slot.status = "failed"
            slot.error_message = "upload terminated"
            db.commit()
            await publish_progress(upload_stream_name(upload_id), {"status": "terminated"})
            await _publish_aggregate(db, slot)
            return JSONResponse({})

        return JSONResponse({})
    finally:
        db.close()

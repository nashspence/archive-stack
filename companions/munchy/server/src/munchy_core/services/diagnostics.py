from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import uuid
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

from time_formats import utc_timestamp_now

import munchy_core.persistence.sqlite_state as state_store
import munchy_core.runtime.config as runtime_config
import munchy_core.runtime.execution as execution_runtime
from munchy_core.domain.errors import ServiceError

DIAGNOSTIC_SORT_COLUMNS = {
    "job_id": "job_id",
    "created_at": "created_at",
    "reason": "reason",
    "bytes": "bytes",
}


def _diagnostic_root() -> Path:
    return (runtime_config.DIAGNOSTIC_DIR / "jobs").resolve()


def _diagnostic_path(job_id: str, created_at: str) -> Path:
    job_key = hashlib.sha256(job_id.encode("utf-8")).hexdigest()
    timestamp = "".join(ch for ch in created_at if ch.isdigit())
    return _diagnostic_root() / job_key / f"{timestamp}.tar.gz"


def _safe_recorded_path(value: str) -> Path:
    path = Path(value)
    root = _diagnostic_root()
    try:
        path.resolve().relative_to(root)
    except ValueError as exc:
        raise RuntimeError("job diagnostic path escapes the diagnostic root") from exc
    if path.is_symlink():
        raise RuntimeError("job diagnostic path must not be a symlink")
    return path


def _add_bytes(archive: tarfile.TarFile, name: str, content: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(content)
    info.mode = 0o600
    archive.addfile(info, io.BytesIO(content))


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def public_diagnostic(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record[key] for key in ("job_id", "created_at", "reason", "bytes", "sha256")}


def create_job_diagnostic(
    job: dict[str, Any],
    *,
    reason: str,
    error: Any | None = None,
) -> dict[str, Any] | None:
    job_id = str(job.get("job_id") or "")
    if not job_id:
        raise RuntimeError("cannot create a diagnostic for a job without an id")
    if str(job.get("state") or "") != "failed":
        raise RuntimeError("job diagnostics are only created for failed jobs")
    with execution_runtime.state_lock:
        if state_store.read_job_diagnostic(job_id) is not None:
            return None

        created_at = utc_timestamp_now()
        output = _diagnostic_path(job_id, created_at)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.part")
        error_text = str(error or job.get("error") or "")
        metadata = {
            "job_id": job_id,
            "state": job.get("state"),
            "phase": job.get("phase"),
            "reason": reason,
            "error": error_text,
            "created_at": created_at,
        }
        try:
            with tarfile.open(temporary, mode="w:gz") as archive:
                _add_bytes(archive, "metadata.json", _json_bytes(metadata))
                if error_text:
                    _add_bytes(archive, "error.txt", (error_text + "\n").encode("utf-8"))
                _add_bytes(archive, "job-state.json", _json_bytes(job))
                for key in (
                    "target_statuses",
                    "target_payloads",
                    "target_result",
                    "target_results",
                    "eager_archive",
                ):
                    value = job.get(key)
                    if value is not None:
                        _add_bytes(archive, f"details/{key}.json", _json_bytes(value))
            os.replace(temporary, output)
            record = {
                "job_id": job_id,
                "created_at": created_at,
                "reason": reason,
                "path": str(output),
                "bytes": output.stat().st_size,
                "sha256": _file_sha256(output),
            }
            try:
                state_store.save_job_diagnostic(record)
            except Exception:
                output.unlink(missing_ok=True)
                raise
            return public_diagnostic(record)
        finally:
            temporary.unlink(missing_ok=True)


def get_job_diagnostic(job_id: str) -> dict[str, Any]:
    record = state_store.read_job_diagnostic(job_id)
    if record is None:
        raise ServiceError(status_code=404, detail=f"job diagnostic not found: {job_id}")
    return public_diagnostic(record)


def job_diagnostic_content(job_id: str) -> tuple[Iterator[bytes], dict[str, Any]]:
    with execution_runtime.state_lock:
        record = state_store.read_job_diagnostic(job_id)
        if record is None:
            raise ServiceError(status_code=404, detail=f"job diagnostic not found: {job_id}")
        path = _safe_recorded_path(str(record["path"]))
        if not path.is_file():
            raise ServiceError(status_code=409, detail=f"job diagnostic file is missing: {job_id}")
        if path.stat().st_size != int(record["bytes"]):
            raise ServiceError(status_code=409, detail=f"job diagnostic size has changed: {job_id}")
        handle = path.open("rb")

    def chunks() -> Iterator[bytes]:
        try:
            yield from iter(lambda: handle.read(1024 * 1024), b"")
        finally:
            handle.close()

    return chunks(), public_diagnostic(record)


def remove_job_diagnostic(job_id: str, *, missing_ok: bool = False) -> dict[str, Any] | None:
    with execution_runtime.state_lock:
        record = state_store.read_job_diagnostic(job_id)
        if record is None:
            if missing_ok:
                return None
            raise ServiceError(status_code=404, detail=f"job diagnostic not found: {job_id}")
        path = _safe_recorded_path(str(record["path"]))
        if path.exists() and not path.is_file():
            raise RuntimeError(f"job diagnostic is not a regular file: {path}")
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass
        state_store.delete_job_diagnostic(job_id)
        return public_diagnostic(record)


def list_job_diagnostics_page(
    *,
    page: int,
    per_page: int,
    sort: str,
    order: str,
    query: str | None,
    all_items: bool = False,
) -> dict[str, Any]:
    bounded_page = max(1, page)
    bounded_per_page = max(1, min(per_page, 100))
    normalized_sort = sort.casefold()
    if normalized_sort not in DIAGNOSTIC_SORT_COLUMNS:
        raise ServiceError(
            status_code=400,
            detail="sort must be one of: " + ", ".join(sorted(DIAGNOSTIC_SORT_COLUMNS)),
        )
    normalized_order = order.casefold()
    if normalized_order not in {"asc", "desc"}:
        raise ServiceError(status_code=400, detail="order must be asc or desc")
    where_sql = ""
    params: list[Any] = []
    if query:
        where_sql = "WHERE instr(lower(job_id || ' ' || reason), lower(?)) > 0"
        params.append(query.strip())
    sort_column = DIAGNOSTIC_SORT_COLUMNS[normalized_sort]
    direction = normalized_order.upper()
    order_sql = f"{sort_column} {direction}, job_id ASC"
    offset = (bounded_page - 1) * bounded_per_page
    limit_sql = "" if all_items else "LIMIT ? OFFSET ?"
    row_params = params if all_items else [*params, bounded_per_page, offset]
    with closing(state_store.state_db()) as conn:
        total = int(
            conn.execute(
                f"SELECT COUNT(*) AS total FROM job_diagnostics {where_sql}",
                params,
            ).fetchone()["total"]
        )
        rows = conn.execute(
            f"""
            SELECT job_id, created_at, reason, bytes, sha256
            FROM job_diagnostics
            {where_sql}
            ORDER BY {order_sql}
            {limit_sql}
            """,
            row_params,
        ).fetchall()
    return {
        "page": 1 if all_items else bounded_page,
        "pages": (1 if total else 0)
        if all_items
        else (total + bounded_per_page - 1) // bounded_per_page
        if total
        else 0,
        "per_page": total if all_items else bounded_per_page,
        "total": total,
        "sort": normalized_sort,
        "order": normalized_order,
        "query": query,
        "diagnostics": [dict(row) for row in rows],
    }


def remove_terminal_job(job_id: str) -> dict[str, Any]:
    with execution_runtime.state_lock:
        if job_id in execution_runtime.active_jobs or job_id in execution_runtime.scheduled_jobs:
            raise ServiceError(status_code=409, detail=f"job is active: {job_id}")
        job = state_store.load_job(job_id)
        if str(job.get("state") or "") not in {"succeeded", "failed", "canceled"}:
            raise ServiceError(status_code=409, detail=f"job is not terminal: {job_id}")
        if not job.get("cleanup_completed_at"):
            raise ServiceError(
                status_code=409,
                detail=f"job local cleanup has not completed: {job_id}",
            )
        diagnostic = remove_job_diagnostic(job_id, missing_ok=True)
        state_store.delete_jobs([job_id])
    return {
        "job_id": job_id,
        "state": str(job.get("state") or ""),
        "diagnostic_removed": diagnostic is not None,
        "removed": True,
    }

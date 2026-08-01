from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, timedelta
from typing import Any

from time_formats import format_utc_timestamp

import munchy_core.persistence.sqlite_state as state_store
import munchy_core.runtime.config as runtime_config
import munchy_core.services.diagnostics as diagnostic_service

PLAN_SAMPLE_SIZE = 10


def _cutoff(retention_seconds: float) -> str:
    return format_utc_timestamp(datetime.now(UTC) - timedelta(seconds=retention_seconds))


def _diagnostic_candidates(cutoff: str, *, limit: int) -> tuple[int, list[str]]:
    with closing(state_store.state_db()) as conn:
        total = int(
            conn.execute(
                "SELECT COUNT(*) AS total FROM job_diagnostics WHERE created_at < ?",
                (cutoff,),
            ).fetchone()["total"]
        )
        rows = conn.execute(
            """
            SELECT job_id
            FROM job_diagnostics
            WHERE created_at < ?
            ORDER BY created_at ASC, job_id ASC
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
    return total, [str(row["job_id"]) for row in rows]


def _terminal_job_candidates(cutoff: str, *, limit: int) -> tuple[int, list[str]]:
    condition = """
        js.terminal = 1
        AND js.updated_at < ?
        AND COALESCE(json_extract(s.payload, '$.cleanup_completed_at'), '') != ''
    """
    with closing(state_store.state_db()) as conn:
        total = int(
            conn.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM job_summaries js
                JOIN states s ON s.kind = 'job' AND s.id = js.job_id
                WHERE {condition}
                """,
                (cutoff,),
            ).fetchone()["total"]
        )
        rows = conn.execute(
            f"""
            SELECT js.job_id
            FROM job_summaries js
            JOIN states s ON s.kind = 'job' AND s.id = js.job_id
            WHERE {condition}
            ORDER BY js.updated_at ASC, js.job_id ASC
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
    return total, [str(row["job_id"]) for row in rows]


def retention_plan(*, candidate_limit: int = PLAN_SAMPLE_SIZE) -> dict[str, Any]:
    terminal_seconds = runtime_config.TERMINAL_JOB_RETENTION.total_seconds()
    diagnostic_seconds = runtime_config.JOB_DIAGNOSTIC_RETENTION.total_seconds()
    terminal_cutoff = _cutoff(terminal_seconds)
    diagnostic_cutoff = _cutoff(diagnostic_seconds)
    terminal_total, terminal_ids = _terminal_job_candidates(
        terminal_cutoff,
        limit=candidate_limit,
    )
    diagnostic_total, diagnostic_ids = _diagnostic_candidates(
        diagnostic_cutoff,
        limit=candidate_limit,
    )
    return {
        "policy": {
            "terminal_job_retention_seconds": int(terminal_seconds),
            "job_diagnostic_retention_seconds": int(diagnostic_seconds),
            "maintenance_batch_size": runtime_config.RETENTION_BATCH_SIZE,
        },
        "terminal_jobs": {
            "cutoff": terminal_cutoff,
            "eligible": terminal_total,
            "sample_job_ids": terminal_ids,
        },
        "job_diagnostics": {
            "cutoff": diagnostic_cutoff,
            "eligible": diagnostic_total,
            "sample_job_ids": diagnostic_ids,
        },
    }


def apply_retention() -> dict[str, Any]:
    before = retention_plan(candidate_limit=runtime_config.RETENTION_BATCH_SIZE)
    diagnostic_ids = list(before["job_diagnostics"]["sample_job_ids"])
    terminal_job_ids = list(before["terminal_jobs"]["sample_job_ids"])
    removed_diagnostics: list[str] = []
    removed_terminal_jobs: list[str] = []
    errors: list[dict[str, str]] = []

    for job_id in diagnostic_ids:
        try:
            if diagnostic_service.remove_job_diagnostic(job_id, missing_ok=True) is not None:
                removed_diagnostics.append(job_id)
        except Exception as exc:
            errors.append({"resource": "job_diagnostic", "job_id": job_id, "error": str(exc)})

    for job_id in terminal_job_ids:
        try:
            diagnostic_service.remove_terminal_job(job_id)
            removed_terminal_jobs.append(job_id)
        except Exception as exc:
            errors.append({"resource": "terminal_job", "job_id": job_id, "error": str(exc)})

    return {
        "policy": before["policy"],
        "removed": {
            "terminal_jobs": len(removed_terminal_jobs),
            "terminal_job_ids": removed_terminal_jobs,
            "job_diagnostics": len(removed_diagnostics),
            "job_diagnostic_ids": removed_diagnostics,
        },
        "errors": errors,
        "remaining": retention_plan(),
    }

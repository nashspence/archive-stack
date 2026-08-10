from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from typing import Any

from pydantic import ValidationError
from time_formats import (
    utc_timestamp_now,
)

import munchy_core.domain.models as domain_models
import munchy_core.persistence.sqlite_state as state_store
from munchy_core.domain.errors import ServiceError
from munchy_core.domain.job_templates import (
    JobTemplateError,
    job_template_digest,
    normalize_job_template,
    render_job_template_inputs,
)


def validated_job_template_definition(
    definition: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str]:
    try:
        normalized, defaults = normalize_job_template(definition)
        input_values = {
            name: str(spec.get("enum", ["template-validation"])[0])
            for name, spec in dict(normalized.get("inputs") or {}).items()
        }
        validation_payload = render_job_template_inputs(
            normalized,
            defaults,
            input_values,
        )
        validation_payload["input_upload_id"] = "template-validation"
        domain_models.CreateJobRequest.model_validate(validation_payload)
    except (JobTemplateError, ValidationError) as exc:
        raise ServiceError(status_code=400, detail=str(exc)) from exc
    return normalized, defaults, job_template_digest(normalized)


def job_template_row_payload(
    row: sqlite3.Row,
    *,
    include_definition: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "template_id": str(row["template_id"]),
        "enabled": bool(row["enabled"]),
        "revision": int(row["revision"]),
        "digest": str(row["digest"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }
    if include_definition:
        payload["definition"] = json.loads(str(row["definition"]))
        payload["resolved_job"] = json.loads(str(row["resolved_job"]))
    return payload


def load_job_template(template_id: str, *, require_enabled: bool = False) -> dict[str, Any]:
    with closing(state_store.state_db()) as conn:
        row = conn.execute(
            "SELECT * FROM job_templates WHERE template_id = ?",
            (template_id,),
        ).fetchone()
    if row is None:
        raise ServiceError(status_code=404, detail=f"unknown job template: {template_id}")
    payload = job_template_row_payload(row, include_definition=True)
    if require_enabled and not payload["enabled"]:
        raise ServiceError(status_code=409, detail=f"job template is disabled: {template_id}")
    return payload


def create_job_template_record(req: domain_models.JobTemplateCreateRequest) -> dict[str, Any]:
    definition, resolved_job, digest = validated_job_template_definition(req.definition)
    now = utc_timestamp_now()
    try:
        with closing(state_store.state_db()) as conn:
            conn.execute(
                """
                INSERT INTO job_templates(
                    template_id, definition, resolved_job, digest, revision, enabled,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    req.template_id,
                    json.dumps(definition, sort_keys=True),
                    json.dumps(resolved_job, sort_keys=True),
                    digest,
                    state_store.bool_int(req.enabled),
                    now,
                    now,
                ),
            )
            conn.commit()
    except sqlite3.IntegrityError as exc:
        raise ServiceError(
            status_code=409,
            detail=f"job template already exists: {req.template_id}",
        ) from exc
    return load_job_template(req.template_id)


def replace_job_template_record(
    template_id: str,
    req: domain_models.JobTemplateReplaceRequest,
) -> dict[str, Any]:
    definition, resolved_job, digest = validated_job_template_definition(req.definition)
    now = utc_timestamp_now()
    with closing(state_store.state_db()) as conn:
        changed = conn.execute(
            """
            UPDATE job_templates
            SET definition = ?, resolved_job = ?, digest = ?, revision = revision + 1,
                enabled = ?, updated_at = ?
            WHERE template_id = ? AND revision = ?
            """,
            (
                json.dumps(definition, sort_keys=True),
                json.dumps(resolved_job, sort_keys=True),
                digest,
                state_store.bool_int(req.enabled),
                now,
                template_id,
                req.expected_revision,
            ),
        ).rowcount
        if not changed:
            exists = conn.execute(
                "SELECT revision FROM job_templates WHERE template_id = ?",
                (template_id,),
            ).fetchone()
            if exists is None:
                raise ServiceError(status_code=404, detail=f"unknown job template: {template_id}")
            raise ServiceError(
                status_code=409,
                detail={
                    "error": "job_template_revision_conflict",
                    "template_id": template_id,
                    "expected_revision": req.expected_revision,
                    "current_revision": int(exists["revision"]),
                },
            )
        conn.commit()
    return load_job_template(template_id)


def set_job_template_enabled_record(
    template_id: str,
    *,
    enabled: bool,
    expected_revision: int,
) -> dict[str, Any]:
    now = utc_timestamp_now()
    with closing(state_store.state_db()) as conn:
        changed = conn.execute(
            """
            UPDATE job_templates
            SET enabled = ?, revision = revision + 1, updated_at = ?
            WHERE template_id = ? AND revision = ?
            """,
            (state_store.bool_int(enabled), now, template_id, expected_revision),
        ).rowcount
        if not changed:
            exists = conn.execute(
                "SELECT revision FROM job_templates WHERE template_id = ?",
                (template_id,),
            ).fetchone()
            if exists is None:
                raise ServiceError(status_code=404, detail=f"unknown job template: {template_id}")
            raise ServiceError(
                status_code=409,
                detail={
                    "error": "job_template_revision_conflict",
                    "template_id": template_id,
                    "expected_revision": expected_revision,
                    "current_revision": int(exists["revision"]),
                },
            )
        conn.commit()
    return load_job_template(template_id)


def delete_job_template_record(template_id: str, *, expected_revision: int) -> dict[str, Any]:
    with closing(state_store.state_db()) as conn:
        changed = conn.execute(
            "DELETE FROM job_templates WHERE template_id = ? AND revision = ?",
            (template_id, expected_revision),
        ).rowcount
        if not changed:
            exists = conn.execute(
                "SELECT revision FROM job_templates WHERE template_id = ?",
                (template_id,),
            ).fetchone()
            if exists is None:
                raise ServiceError(status_code=404, detail=f"unknown job template: {template_id}")
            raise ServiceError(
                status_code=409,
                detail={
                    "error": "job_template_revision_conflict",
                    "template_id": template_id,
                    "expected_revision": expected_revision,
                    "current_revision": int(exists["revision"]),
                },
            )
        conn.commit()
    return {"template_id": template_id, "state": "removed"}


def list_job_templates_page(
    *,
    page: int,
    per_page: int,
    sort: str,
    order: str,
    query: str | None,
    enabled: bool | None,
    all_items: bool = False,
) -> dict[str, Any]:
    bounded_page = max(1, page)
    bounded_per_page = max(1, min(per_page, 100))
    normalized_sort = sort.casefold()
    if normalized_sort not in domain_models.JOB_TEMPLATE_LIST_SORT_COLUMNS:
        raise ServiceError(
            status_code=400,
            detail="sort must be one of: "
            + ", ".join(sorted(domain_models.JOB_TEMPLATE_LIST_SORT_COLUMNS)),
        )
    normalized_order = order.casefold()
    if normalized_order not in {"asc", "desc"}:
        raise ServiceError(status_code=400, detail="order must be asc or desc")
    where: list[str] = []
    params: list[Any] = []
    if query:
        escaped = query.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("lower(template_id) LIKE ? ESCAPE '\\'")
        params.append(f"%{escaped}%")
    if enabled is not None:
        where.append("enabled = ?")
        params.append(state_store.bool_int(enabled))
    where_sql = " WHERE " + " AND ".join(where) if where else ""
    sort_column = domain_models.JOB_TEMPLATE_LIST_SORT_COLUMNS[normalized_sort]
    direction = normalized_order.upper()
    offset = (bounded_page - 1) * bounded_per_page
    limit_sql = "" if all_items else "LIMIT ? OFFSET ?"
    row_params = params if all_items else [*params, bounded_per_page, offset]
    with closing(state_store.state_db()) as conn:
        total = int(
            conn.execute(
                f"SELECT COUNT(*) AS total FROM job_templates{where_sql}",
                params,
            ).fetchone()["total"]
        )
        rows = conn.execute(
            f"""
            SELECT * FROM job_templates
            {where_sql}
            ORDER BY {sort_column} {direction}, template_id ASC
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
        "filters": {"enabled": enabled},
        "templates": [job_template_row_payload(row, include_definition=False) for row in rows],
    }


def submission_template_summary(job: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: job[key]
        for key in ("template_id", "template_revision", "template_digest")
        if key in job
    }

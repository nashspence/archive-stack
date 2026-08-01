from __future__ import annotations

import hashlib
from typing import Any

from lifecycle_events import (
    cloud_event,
    normalize_event_context,
)
from lifecycle_events.repeats import (
    event_repeat_due,
)
from time_formats import (
    format_utc_timestamp,
    utc_now,
)

import munchy_core.domain.models as domain_models
import munchy_core.persistence.lifecycle_events as lifecycle_store
import munchy_core.persistence.sqlite_state as state_store
import munchy_core.runtime.config as runtime_config


def event_context_expiry() -> str:
    return format_utc_timestamp(utc_now() + runtime_config.EVENT_CONTEXT_RETENTION)


def emit_job_event(
    job: dict[str, Any],
    event: domain_models.LifecycleEventType,
    _summary: str,
    *,
    severity: str = "info",
    extra: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
    fingerprint: str | None = None,
) -> dict[str, Any] | None:
    key = dedupe_key or event
    emissions = job.setdefault("event_emissions", {})
    event_state = emissions.setdefault(key, {})
    now = utc_now()
    now_text = format_utc_timestamp(now)
    details = dict(extra or {})

    if event == "job.issue":
        last_fingerprint = str(event_state.get("fingerprint") or "")
        last_attempt = state_store.safe_parse_timestamp(event_state.get("last_attempt_at"))
        if (
            fingerprint
            and fingerprint == last_fingerprint
            and last_attempt is not None
            and not event_repeat_due(
                last_emitted_at=last_attempt,
                current=now,
                interval=runtime_config.EVENT_REPEAT_INTERVAL_SECONDS,
                repeat_time=runtime_config.EVENT_REPEAT_TIME,
                repeat_timezone=runtime_config.EVENT_REPEAT_TIMEZONE,
            )
        ):
            return {"status": "suppressed", "reason": "issue_repeat_limit"}
        event_state["fingerprint"] = fingerprint or ""
        event_state["last_attempt_at"] = now_text
    elif event == "job.upload_stalled":
        interval = max(0, runtime_config.EVENT_REPEAT_INTERVAL_SECONDS)
        if interval <= 0:
            return {"status": "suppressed", "reason": "reminders_disabled"}
        last_attempt = state_store.safe_parse_timestamp(event_state.get("last_attempt_at"))
        if last_attempt is not None and not event_repeat_due(
            last_emitted_at=last_attempt,
            current=now,
            interval=interval,
            repeat_time=runtime_config.EVENT_REPEAT_TIME,
            repeat_timezone=runtime_config.EVENT_REPEAT_TIMEZONE,
        ):
            return {"status": "suppressed", "reason": "reminder_repeat_limit"}
        event_state["last_attempt_at"] = now_text
        reminder_count = int(event_state.get("reminder_count") or 0) + 1
        event_state["reminder_count"] = reminder_count
        details.setdefault("repeat_count", reminder_count)
        details.setdefault("repeat_interval_seconds", interval)
    elif event_state.get("emitted_at"):
        return {"status": "suppressed", "reason": "already_emitted"}
    else:
        event_state["last_attempt_at"] = now_text

    job_id = str(job.get("job_id") or "")
    owner = str(job.get("initiated_by_app") or "munchy")
    data: dict[str, Any] = {
        "job_id": job_id,
        "template_id": str(job.get("template_id") or ""),
        "template_revision": job.get("template_revision"),
        "template_digest": str(job.get("template_digest") or ""),
        "job_created_at": str(job.get("created_at") or ""),
        "state": str(job.get("state") or "unknown"),
        "phase": str(job.get("phase") or "unknown"),
        "workflow_mode": str(job.get("workflow_mode") or ""),
        "run_id": str(job.get("run_id") or ""),
        "severity": severity,
        "actor": {"app": "munchy"},
        "initiator": {
            "app": owner,
            "key_id": job.get("initiated_by_key_id"),
        },
    }
    data.update(details)
    context = normalize_event_context(job.get("event_context"))
    terminal = str(job.get("state") or "") in domain_models.TERMINAL_JOB_STATES
    expiry = event_context_expiry() if terminal and context is not None else None
    event_record = cloud_event(
        source=runtime_config.EVENT_SOURCE,
        type=f"io.riverhog.munchy.{event}",
        subject=job_id or None,
        data=data,
    )
    event_log = lifecycle_store.lifecycle_event_log()
    event_log.initialize()
    if terminal and job_id:
        event_log.expire_context(owner=owner, subject=job_id, expires_at=expiry or now_text)
    cursor = event_log.append(
        event_record,
        owner=owner,
        context=context,
        context_expires_at=expiry,
    )
    event_state["cursor"] = cursor
    event_state["emitted_at"] = now_text
    if event in {"job.issue", "job.upload_stalled"}:
        event_state["last_sent_at"] = now_text
    state_store.save_job(job)
    return {"status": "emitted", "cursor": cursor, "event_id": event_record.id}


def emit_job_issue(
    job: dict[str, Any],
    *,
    component: str,
    error: Exception | str,
    severity: str = "warning",
    attempt: int | None = None,
    next_retry_at: str | None = None,
) -> dict[str, Any] | None:
    error_text = str(error)
    fingerprint = hashlib.sha256(f"{component}:{error_text}".encode()).hexdigest()
    extra: dict[str, Any] = {
        "component": component,
        "error": error_text[-1000:],
    }
    if attempt is not None:
        extra["attempt"] = attempt
    if next_retry_at:
        extra["next_retry_at"] = next_retry_at
    return emit_job_event(
        job,
        "job.issue",
        f"{component} needs attention: {error_text[-240:]}",
        severity=severity,
        extra=extra,
        dedupe_key=f"job.issue:{component}",
        fingerprint=fingerprint,
    )

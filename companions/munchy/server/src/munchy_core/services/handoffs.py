from __future__ import annotations

import logging
import logging.config
import time
from collections.abc import Callable, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from time_formats import (
    format_utc_timestamp,
    utc_now,
    utc_timestamp_now,
)

import munchy_core.domain.errors as domain_errors
import munchy_core.domain.models as domain_models
import munchy_core.persistence.sqlite_state as state_store
import munchy_core.ports.handoff as handoff_port
import munchy_core.runtime.config as runtime_config
import munchy_core.runtime.execution as execution_runtime
import munchy_core.services.scheduling as scheduling_service

log = logging.getLogger("munchy.server")


def retry_sleep(seconds: float, *, job_id: str | None = None) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        if job_id is not None:
            state_store.raise_if_job_canceled(job_id)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(5.0, remaining))


def retry_handoff_until_success(
    job: dict[str, Any],
    *,
    result_key: str,
    phase: str,
    action: str,
    component: str,
    operation: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    existing = job.get(result_key)
    if isinstance(existing, dict):
        return existing
    delay = max(1.0, runtime_config.HANDOFF_RETRY_INITIAL_SECONDS)
    max_delay = max(delay, runtime_config.HANDOFF_RETRY_MAX_SECONDS)
    job_id = str(job["job_id"])
    while True:
        state_store.raise_if_job_canceled(job_id)
        latest = state_store.read_state("job", job_id)
        if isinstance(latest, dict):
            job.clear()
            job.update(latest)
            existing = job.get(result_key)
            if isinstance(existing, dict):
                return existing
        attempts = job.setdefault("handoff_attempts", {})
        attempt = int(attempts.get(result_key) or 0) + 1
        attempts[result_key] = attempt
        attempts[f"{result_key}_last_attempt_at"] = utc_timestamp_now()
        job["phase"] = phase if attempt == 1 else f"{phase}_retrying"
        state_store.save_job(job)
        try:
            result = operation()
            result["attempt"] = attempt
            result["succeeded_at"] = utc_timestamp_now()
            job[result_key] = result
            job["phase"] = phase
            attempts[f"{result_key}_succeeded_at"] = result["succeeded_at"]
            attempts.pop(f"{result_key}_next_retry_at", None)
            attempts.pop(f"{result_key}_last_error", None)
            state_store.save_job(job)
            return result
        except (domain_errors.HandoffFailed, domain_errors.JobCanceled):
            raise
        except Exception as exc:
            next_retry_at = format_utc_timestamp(utc_now() + timedelta(seconds=delay))
            attempts[f"{result_key}_last_error"] = str(exc)
            attempts[f"{result_key}_next_retry_at"] = next_retry_at
            job["phase"] = f"{phase}_retrying"
            state_store.save_job(job)
            log.warning(
                "%s attempt %s failed; retrying at %s: %s", action, attempt, next_retry_at, exc
            )
            retry_sleep(delay, job_id=job_id)
            delay = min(max_delay, delay * 2)


def handoff_on_failure(job: dict[str, Any]) -> str:
    value = str(
        state_store.dict_or_empty(job.get("handoff")).get("on_failure") or "preserve_for_resume"
    )
    if value not in {"preserve_for_resume", "cancel"}:
        return "preserve_for_resume"
    return value


def should_cancel_handoff_on_failure(job: dict[str, Any], exc: Exception) -> bool:
    if isinstance(exc, domain_errors.EncodingFailed):
        return True
    if handoff_on_failure(job) == "cancel":
        return True
    return not handoff_adapter(job).can_resume(job)


def eager_handoff_candidate_jobs() -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for job in scheduling_service.job_states():
        try:
            adapter = handoff_adapter(job)
        except RuntimeError:
            continue
        if adapter.supports_eager and adapter.eager_ready(job):
            candidates.append(job)
    candidates.sort(key=lambda item: str(item.get("created_at") or ""))
    return candidates


def handoff_loop() -> None:
    while not execution_runtime.handoff_stop.wait(eager_handoff_interval_seconds()):
        try:
            for job in eager_handoff_candidate_jobs():
                if execution_runtime.handoff_stop.is_set():
                    return
                archive_dir = (
                    runtime_config.GPU_RUNTIME_DIR
                    / "jobs"
                    / str(job.get("job_id") or "")
                    / "archive"
                )
                advance_handoff(
                    job,
                    archive_dir,
                    final=False,
                    source_label="collection archive",
                )
        except domain_errors.JobCanceled as exc:
            log.info("eager handoff worker noticed cancellation: %s", exc)
        except Exception:
            log.exception("eager handoff worker failed")


def handoff_config(job: dict[str, Any]) -> dict[str, Any]:
    configured = job.setdefault("handoff", {})
    if not isinstance(configured, dict):
        raise RuntimeError("job handoff config is invalid")
    return configured


def register_handoff_adapter(
    adapter: handoff_port.HandoffAdapter,
    *,
    option_model: type[BaseModel],
) -> None:
    handoff_port.HANDOFF_ADAPTERS[adapter.name] = adapter
    domain_models.HANDOFF_OPTION_MODELS[adapter.name] = option_model


def eager_handoff_interval_seconds() -> float:
    intervals = [
        adapter.eager_interval_seconds
        for adapter in handoff_port.HANDOFF_ADAPTERS.values()
        if adapter.supports_eager
    ]
    return min(intervals, default=1.0)


def optional_handoff_adapter(job: dict[str, Any]) -> handoff_port.HandoffAdapter | None:
    destination = str(handoff_config(job).get("destination") or "")
    if not destination:
        return None
    adapter = handoff_port.HANDOFF_ADAPTERS.get(destination)
    if adapter is None:
        raise RuntimeError(f"unsupported handoff destination: {destination}")
    return adapter


def handoff_adapter(job: dict[str, Any]) -> handoff_port.HandoffAdapter:
    adapter = optional_handoff_adapter(job)
    if adapter is None:
        raise RuntimeError("unsupported handoff destination: missing")
    return adapter


def advance_handoff(
    job: dict[str, Any],
    source_dir: Path,
    *,
    final: bool,
    source_label: str,
    context: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    return handoff_adapter(job).advance(
        job,
        source_dir,
        final=final,
        source_label=source_label,
        context=context,
    )


def cancel_handoff(job: dict[str, Any], *, reason: str) -> None:
    adapter = handoff_adapter(job)
    adapter.cancel(job, reason=reason)
    configured = handoff_config(job)
    configured["state"] = "canceled"
    configured["safe_to_delete"] = False
    state_store.save_job(job)


def refresh_handoff(job: dict[str, Any]) -> None:
    handoff_adapter(job).refresh(job)


def handoff_safe_to_delete(job: dict[str, Any]) -> bool:
    return handoff_adapter(job).safe_to_delete(job)


def current_handoff_progress(job: dict[str, Any]) -> dict[str, Any] | None:
    return handoff_adapter(job).progress(job)

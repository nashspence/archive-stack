"""Command and rclone handoff adapters for Munchy."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from munchy_workflows.platform_files import (
    DEFAULT_PLATFORM_CRUFT_EXCLUDES,
    normalize_exclude_patterns,
    path_matches_exclude_patterns,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator

from munchy_core import coordinator as core

HANDOFF_ATTEMPTS = int(os.getenv("MUNCHY_HANDOFF_ATTEMPTS", "3"))
EXTERNAL_HANDOFF_ENABLED = os.getenv("MUNCHY_EXTERNAL_HANDOFF_ENABLED", "0").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
COMMAND_HANDOFF_COMMAND = os.getenv("MUNCHY_COMMAND_HANDOFF_COMMAND", "").strip()
RCLONE_HANDOFF_COMMAND = os.getenv("MUNCHY_RCLONE_HANDOFF_COMMAND", "rclone")


class CommandHandoffOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exclude: list[str] = Field(default_factory=list)

    @field_validator("exclude")
    @classmethod
    def normalize_exclude(cls, value: list[str]) -> list[str]:
        return normalize_exclude_patterns(value, label="command handoff exclude")


class RcloneHandoffOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    location: str = Field(min_length=1, max_length=4096)
    mode: Literal["copy", "sync"] = "copy"
    exclude: list[str] = Field(default_factory=list)

    @field_validator("location")
    @classmethod
    def normalize_location(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("rclone handoff location must not be blank")
        return normalized

    @field_validator("exclude")
    @classmethod
    def normalize_exclude(cls, value: list[str]) -> list[str]:
        return normalize_exclude_patterns(value, label="rclone handoff exclude")


def render_job_template(
    value: str,
    job: dict[str, Any],
    *,
    context: Mapping[str, str] | None = None,
) -> str:
    review = core.dict_or_empty(job.get("review"))
    fields = {
        "job_id": str(job.get("job_id") or ""),
        "run_id": str(job.get("run_id") or ""),
        "template_id": str(job.get("template_id") or ""),
        "route_id": str(review.get("route_id") or ""),
        "profile_id": str(review.get("profile_id") or ""),
    }
    if context is not None:
        fields.update({str(key): str(item) for key, item in context.items()})
    try:
        return value.format(**fields)
    except KeyError as exc:
        raise RuntimeError(f"unknown handoff template field: {exc.args[0]}") from exc


def handoff_excludes(config: Mapping[str, Any]) -> list[str]:
    excludes = list(DEFAULT_PLATFORM_CRUFT_EXCLUDES)
    raw_excludes = config.get("exclude") or []
    if not isinstance(raw_excludes, Sequence) or isinstance(raw_excludes, (str, bytes)):
        raise RuntimeError("handoff exclude must be a list")
    if not all(isinstance(item, str) for item in raw_excludes):
        raise RuntimeError("handoff exclude entries must be strings")
    try:
        extra_excludes = normalize_exclude_patterns(
            list(raw_excludes),
            label="handoff exclude",
        )
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    for pattern in extra_excludes:
        if pattern not in excludes:
            excludes.append(pattern)
    return excludes


def handoff_artifact_count(source_dir: Path, *, excludes: Sequence[str] = ()) -> int:
    if not source_dir.is_dir():
        return 0
    return sum(
        1
        for path in source_dir.rglob("*")
        if path.is_file()
        and not path_matches_exclude_patterns(path.relative_to(source_dir).as_posix(), excludes)
    )


def run_external_handoff(
    job: dict[str, Any],
    source_dir: Path,
    *,
    config: Mapping[str, Any] | None = None,
    source_label: str = "review",
    result_key: str = "handoff_receipt",
    phase: str = "handoff",
    component: str = "handoff",
    event: core.LifecycleEventType = "review.handoff",
    allow_empty: bool = True,
    emit_event: bool = True,
    template_context: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    if config is None:
        raise RuntimeError("external handoff config is required")
    if not config.get("enabled"):
        return None
    if not EXTERNAL_HANDOFF_ENABLED:
        raise RuntimeError("external handoff requested, but external handoffs are disabled")
    excludes = handoff_excludes(config)
    artifact_count = handoff_artifact_count(source_dir, excludes=excludes)
    if artifact_count == 0:
        if not allow_empty:
            raise RuntimeError(f"{source_label} artifacts are empty: {source_dir}")
        return {
            "status": "skipped",
            "reason": f"no {source_label} artifacts",
            "source": str(source_dir),
        }
    method = str(config.get("method") or "command")
    if emit_event:
        core.emit_job_event(
            job,
            event,
            f"{source_label.title()} artifacts are complete; handing off for upload.",
            extra={
                "source_dir": str(source_dir),
                "source_label": source_label,
                "method": method,
                "artifact_count": artifact_count,
            },
        )
    if method == "rclone":
        destination = str(config.get("destination") or "").strip()
        if not destination:
            raise RuntimeError("rclone handoff location is required")
        rendered_destination = render_job_template(destination, job, context=template_context)
        mode = str(config.get("mode") or "copy")
        if mode not in {"copy", "sync"}:
            raise RuntimeError(f"unsupported rclone handoff mode: {mode}")
        command = [
            RCLONE_HANDOFF_COMMAND,
            mode,
            *[arg for pattern in excludes for arg in ("--exclude", pattern)],
            str(source_dir),
            rendered_destination,
            "--retries",
            str(max(1, HANDOFF_ATTEMPTS)),
            "--low-level-retries",
            "10",
            "--stats",
            "30s",
        ]

        def rclone_operation() -> dict[str, Any]:
            result = core.run_command(command, action=f"{source_label} rclone handoff")
            result.update(
                {
                    "destination": "rclone",
                    "mode": mode,
                    "source": str(source_dir),
                    "source_label": source_label,
                    "location": rendered_destination,
                    "artifact_count": artifact_count,
                }
            )
            return result

        return core.retry_handoff_until_success(
            job,
            result_key=result_key,
            phase=phase,
            action=f"{source_label} rclone handoff",
            component=component,
            operation=rclone_operation,
        )
    if method != "command":
        raise RuntimeError(f"unsupported external handoff adapter: {method}")
    if not COMMAND_HANDOFF_COMMAND:
        raise RuntimeError("command handoff requested, but MUNCHY_COMMAND_HANDOFF_COMMAND is empty")
    environment = os.environ.copy()
    environment["MUNCHY_HANDOFF_SOURCE"] = str(source_dir)
    environment["MUNCHY_HANDOFF_SOURCE_LABEL"] = source_label
    environment["MUNCHY_JOB_ID"] = str(job["job_id"])
    environment["MUNCHY_RUN_ID"] = str(job.get("run_id") or "")
    review = core.dict_or_empty(job.get("review"))
    review_context = {
        "template_id": str(job.get("template_id") or ""),
        "route_id": str(review.get("route_id") or ""),
        "profile_id": str(review.get("profile_id") or ""),
    }
    if template_context is not None:
        review_context.update({str(key): str(item) for key, item in template_context.items()})
    environment["MUNCHY_TEMPLATE_ID"] = review_context["template_id"]
    environment["MUNCHY_REVIEW_ROUTE_ID"] = review_context["route_id"]
    environment["MUNCHY_REVIEW_PROFILE_ID"] = review_context["profile_id"]

    def command_operation() -> dict[str, Any]:
        result = core.run_command(
            ["/bin/sh", "-lc", COMMAND_HANDOFF_COMMAND],
            action=f"{source_label} command handoff",
            env=environment,
        )
        result.update(
            {
                "destination": "command",
                "source": str(source_dir),
                "source_label": source_label,
                "artifact_count": artifact_count,
            }
        )
        return result

    return core.retry_handoff_until_success(
        job,
        result_key=result_key,
        phase=phase,
        action=f"{source_label} command handoff",
        component=component,
        operation=command_operation,
    )


class ExternalHandoffAdapter:
    enabled = True
    supports_eager = False
    eager_interval_seconds = 1.0

    def __init__(self, name: Literal["command", "rclone"]) -> None:
        self.name: str = name

    def start(self) -> None:
        return

    def stop(self) -> None:
        return

    def advance(
        self,
        job: dict[str, Any],
        source_dir: Path,
        *,
        final: bool,
        source_label: str,
        context: Mapping[str, str] | None = None,
    ) -> dict[str, Any] | None:
        if not final:
            return None
        configured = core.handoff_config(job)
        options = core.dict_or_empty(configured.get("options"))
        upload_config: dict[str, Any] = {**options, "enabled": True, "method": self.name}
        if self.name == "rclone":
            upload_config["destination"] = options.get("location")
        configured["state"] = "transferring"
        core.save_job(job)
        event: core.LifecycleEventType = (
            "review.handoff"
            if str(job.get("workflow_mode") or "") == "review"
            else "collection_archive.handoff"
        )
        receipt = run_external_handoff(
            job,
            source_dir,
            config=upload_config,
            source_label=source_label,
            event=event,
            allow_empty=str(job.get("workflow_mode") or "") == "review",
            emit_event=context is None,
            template_context=context,
        )
        configured = core.handoff_config(job)
        configured["state"] = "complete"
        configured["safe_to_delete"] = True
        core.save_job(job)
        return receipt

    def cancel(self, job: dict[str, Any], *, reason: str) -> None:
        del job, reason

    def refresh(self, job: dict[str, Any]) -> None:
        del job

    def progress(self, job: dict[str, Any]) -> dict[str, Any]:
        configured = core.handoff_config(job)
        state = str(configured.get("state") or "pending")
        return {
            "destination": self.name,
            "state": state,
            "completed": state == "complete",
            "safe_to_delete": bool(configured.get("safe_to_delete")),
            "stages": [{"id": "transfer", "label": f"{self.name.title()} Handoff", "state": state}],
        }

    def safe_to_delete(self, job: dict[str, Any]) -> bool:
        return bool(core.handoff_config(job).get("safe_to_delete"))

    def eager_ready(self, job: dict[str, Any]) -> bool:
        del job
        return False

    def wait_until_idle(self, job: dict[str, Any]) -> None:
        del job

    def can_resume(self, job: dict[str, Any]) -> bool:
        del job
        return False

    def merge_state(self, current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
        return {**current, **incoming}

    def expected_primary_files_total(
        self,
        input_upload: dict[str, Any],
        groups: dict[str, dict[str, Any]],
        routing: Mapping[str, Any] | None,
    ) -> None:
        del input_upload, groups, routing

    def handed_off_paths(self, job: dict[str, Any]) -> set[str]:
        del job
        return set()

    def artifact_record(self, job: dict[str, Any], path: str) -> None:
        del job, path

    def artifact_complete(self, record: dict[str, Any]) -> bool:
        del record
        return False

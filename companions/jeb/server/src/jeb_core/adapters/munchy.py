"""Munchy target delivery and lifecycle-event translation for Jeb."""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from typing import Any

from lifecycle_events import CloudEvent, caused_event, normalize_event_context
from munchy_api_client.client import (
    JobTerminalDuringUpload,
    MunchyClient,
    SubmissionInputFile,
    SubmissionUploadRequest,
    compact_job_failure,
    is_transient_upload_error,
    job_finished_cleanly,
)

from jeb_core.domain.models import (
    EligibleFile,
    TargetConfig,
    UnrecoverableJebError,
    current_time,
    event_timestamp,
    run_id_for,
)
from jeb_core.domain.sources import SourceConfig, SourceRegistryError
from jeb_core.ports.target import TargetContext

LOG = logging.getLogger("jeb.adapters.munchy")
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


class MunchyTargetAdapter:
    """Deliver Jeb attempts to Munchy and translate their resulting events."""

    name = "munchy"
    _REMOTE_STATES = {
        "target_submitted",
        "target_uploading",
        "target_uploaded",
        "target_complete",
        "cleanup_pending",
        "cleanup_failed",
    }

    def start(self, context: TargetContext) -> None:
        threading.Thread(
            target=self.consume_events_forever,
            args=(context,),
            name="munchy-event-loop",
            daemon=True,
        ).start()

    def is_transient_error(self, error: BaseException) -> bool:
        return is_transient_upload_error(error)

    def normalize_source_config(self, config: Mapping[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(config) - {"template_id"})
        if unknown:
            raise SourceRegistryError("unknown Munchy target option(s): " + ", ".join(unknown))
        template_id = str(config.get("template_id") or "").strip()
        if not template_id or len(template_id) > 160 or not SAFE_NAME.fullmatch(template_id):
            raise SourceRegistryError("Munchy target option template_id must be a safe ID")
        return {"template_id": template_id}

    def preflight(
        self,
        context: TargetContext,
        source: SourceConfig,
        files: Sequence[EligibleFile],
        *,
        record_failures: bool,
    ) -> tuple[list[EligibleFile] | None, dict[str, Any]]:
        config = self.normalize_source_config(source.target_config)
        if not files:
            return [], {
                "ok": True,
                "status": "no_files",
                "file_count": 0,
                "target_config": config,
            }
        target = context.target_by_name(source.target)
        request = SubmissionUploadRequest(
            submission_id=f"preflight-{source.id}",
            template_id=str(config["template_id"]),
            files=tuple(
                SubmissionInputFile(
                    source=item.path,
                    rel_path=item.target_path,
                    bytes=item.bytes,
                    sha256="",
                )
                for item in files
            ),
            run_id=run_id_for(),
        )
        with closing(MunchyClient(target.url, token=target.token)) as client:
            try:
                result = client.preflight_submission(request)
            except Exception as exc:
                if self.is_transient_error(exc):
                    LOG.warning(
                        "source %s target preflight hit a transient issue; will retry later: %s",
                        source.id,
                        exc,
                    )
                    return None, {
                        "ok": False,
                        "status": "transient_error",
                        "file_count": len(files),
                        "target_config": config,
                        "error": str(exc),
                    }
                if record_failures:
                    context.record_target_preflight_failure(
                        source=source,
                        files=files,
                        error=exc,
                    )
                    context.emit_target_preflight_failures(source_id=source.id)
                return None, {
                    "ok": False,
                    "status": "rejected",
                    "file_count": len(files),
                    "target_config": config,
                    "error": str(exc),
                }
            accepted = bool(result.get("accepted"))
            summary = {
                "ok": accepted,
                "status": "accepted" if accepted else "rejected",
                "file_count": len(files),
                "target_config": config,
                "result": result,
            }
            if accepted:
                if record_failures:
                    context.clear_target_preflight_failure(source.id)
                return list(files), summary
            error = UnrecoverableJebError("target rejected submission preflight")
            if record_failures:
                context.record_target_preflight_failure(
                    source=source,
                    files=files,
                    error=error,
                )
                context.emit_target_preflight_failures(source_id=source.id)
            return None, summary

    def advance(self, context: TargetContext, attempt_id: str) -> None:
        attempt = context.load_attempt(attempt_id)
        if attempt["state"] == "target_complete":
            return
        target = context.target_by_name(str(attempt["target_name"]))
        with closing(MunchyClient(target.url, token=target.token)) as client:
            request = self.submission_request(context, attempt_id, target)
            state = str(attempt["state"])
            if state == "preflighted":
                client.create_submission(request)
                context.set_attempt_state(attempt_id, "target_submitted")
                state = "target_submitted"
            if state in {"target_submitted", "target_uploading"}:
                context.set_attempt_state(attempt_id, "target_uploading")
                try:
                    client.upload_files(request)
                except JobTerminalDuringUpload as exc:
                    if job_finished_cleanly(exc.job):
                        context.set_attempt_state(attempt_id, "target_complete")
                        return
                    raise UnrecoverableJebError(compact_job_failure(exc.job)) from exc
                context.set_attempt_state(attempt_id, "target_uploaded")
                state = "target_uploaded"
            if state == "target_uploaded":
                submission = client.wait_for_submission(
                    request.submission_id,
                    wait_for_safe_delete=target.wait_for_safe_delete,
                )
                job = submission.get("job")
                if not isinstance(job, dict):
                    raise UnrecoverableJebError(
                        f"Munchy submission returned invalid job state: {submission}"
                    )
                if not job_finished_cleanly(job):
                    raise UnrecoverableJebError(compact_job_failure(job))
                context.set_attempt_state(attempt_id, "target_complete")

    def cancel(self, context: TargetContext, attempt_id: str) -> None:
        attempt = context.load_attempt(attempt_id)
        if str(attempt["state"]) not in self._REMOTE_STATES:
            return
        submission_id = str(attempt["target_submission_id"] or "")
        if not submission_id:
            raise UnrecoverableJebError(
                f"active target delivery has no cancellation identity: {attempt_id}"
            )
        target = context.target_by_name(str(attempt["target_name"]))
        with closing(MunchyClient(target.url, token=target.token)) as client:
            client.cancel_submission(submission_id)

    def submission_request(
        self,
        context: TargetContext,
        attempt_id: str,
        target: TargetConfig,
    ) -> SubmissionUploadRequest:
        attempt = context.load_attempt(attempt_id)
        source = context.source_by_id(str(attempt["source_id"]))
        config = self.normalize_source_config(source.target_config)
        files = tuple(
            SubmissionInputFile(
                source=Path(str(row["staging_path"])),
                rel_path=str(row["target_path"]),
                bytes=int(row["bytes"]),
                sha256=str(row["sha256"]),
            )
            for row in context.attempt_files(attempt_id)
        )
        return SubmissionUploadRequest(
            submission_id=str(attempt["target_submission_id"]),
            template_id=str(config["template_id"]),
            files=files,
            run_id=str(attempt["run_id"]),
            event_context={"initiator": {"app": "jeb", "attempt_id": attempt_id}},
            upload_workers=target.upload_workers,
            upload_chunk_mib=max(1, target.upload_chunk_bytes // (1024 * 1024)),
        )

    def consume_events_forever(self, context: TargetContext) -> None:
        target = context.target_by_name(self.name)
        with closing(MunchyClient(target.url, token=target.token)) as client:
            while True:
                try:
                    if self.consume_events_once(context, client):
                        continue
                except Exception:
                    LOG.exception("Munchy lifecycle event consumption failed")
                context.sleep(context.config.events.upstream_poll_seconds)

    def consume_events_once(self, context: TargetContext, client: MunchyClient) -> int:
        cursor = context.event_cursors.cursor(self.name)
        page = client.list_lifecycle_events(after=cursor, limit=100)
        translated = sum(1 for event in page.events if self.translate_event(context, event))
        if page.next_cursor != cursor:
            context.event_cursors.advance(self.name, page.next_cursor)
        return translated

    def translate_event(self, context: TargetContext, event: CloudEvent) -> bool:
        prefix = "io.riverhog.munchy."
        if not event.type.startswith(prefix):
            return False
        job_id = str(event.data.get("job_id") or event.subject or "")
        if not job_id:
            return False
        with context.connect() as conn:
            rows = conn.execute(
                """
                SELECT a.id AS attempt_id, a.state, b.source_id, b.target_name, b.run_id
                FROM batch_attempts a
                JOIN batches b ON b.id = a.batch_id
                WHERE a.target_submission_id = ?
                ORDER BY a.created_at DESC
                LIMIT 2
                """,
                (job_id,),
            ).fetchall()
        if not rows:
            return False
        if len(rows) > 1:
            raise RuntimeError(f"multiple Jeb attempts claim Munchy job {job_id}")
        row = rows[0]
        suffix = event.type.removeprefix(prefix).removeprefix("job.")
        details = {
            key: value
            for key, value in event.data.items()
            if key not in {"actor", "cause", "context"}
        }
        details.update(
            {
                "actor": {"app": "jeb"},
                "attempt_id": str(row["attempt_id"]),
                "source_id": str(row["source_id"]),
                "state": str(row["state"]),
                "target": str(row["target_name"]),
                "run_id": str(row["run_id"]),
                "target_submission_id": job_id,
            }
        )
        translated = caused_event(
            cause=event,
            source=context.config.events.source,
            type=f"io.riverhog.jeb.attempt.target.{suffix}",
            subject=str(row["attempt_id"]),
            data=details,
        )
        event_context = normalize_event_context(event.data.get("context"))
        context_expires_at = (
            event_timestamp(
                current_time() + timedelta(seconds=context.config.events.context_retention_seconds)
            )
            if event_context is not None
            else None
        )
        context.event_log.append_once(
            translated,
            owner="jeb",
            context=event_context,
            context_expires_at=context_expires_at,
        )
        return True

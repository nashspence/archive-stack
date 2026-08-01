from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from typing import Any

from lifecycle_events import (
    SQLiteLifecycleEventLog,
    cloud_event,
)
from lifecycle_events.repeats import (
    event_repeat_due,
)
from time_formats import parse_utc_timestamp

import jeb_core.domain.models as domain_models
import jeb_core.persistence.sqlite_state as state_store
from jeb_core.domain.models import (
    current_time,
    event_timestamp,
    run_id_for,
)


class JebEventService:
    def __init__(
        self,
        config: domain_models.JebConfig,
        store: state_store.SQLiteJebStore,
        event_log: SQLiteLifecycleEventLog,
    ) -> None:
        self.config = config
        self.store = store
        self.event_log = event_log

    def emit_issue(
        self,
        *,
        context: Mapping[str, Any],
        error: str,
        component: str,
        severity: str,
    ) -> bool:
        attempt_id = str(context.get("id") or "")
        source_id = str(context.get("source_id") or "")
        event_kind = (
            "source.preflight_failed" if component == "target_preflight" else "attempt.issue"
        )
        subject = attempt_id or source_id or None
        data = {
            "component": component,
            "error": error,
            "severity": severity,
            "source_id": source_id,
            "attempt_id": attempt_id if attempt_id and attempt_id != source_id else "",
            "state": str(context.get("state") or "failed"),
            "target": str(context.get("target_name") or context.get("target") or ""),
            "run_id": str(context.get("run_id") or ""),
        }
        event = cloud_event(
            source=self.config.events.source,
            type=f"io.riverhog.jeb.{event_kind}",
            subject=subject,
            data=data,
        )
        self.event_log.append(event, owner="jeb")
        return True

    def emit_target_preflight_failure(self, row: sqlite3.Row) -> bool:
        row_payload = dict(row)
        fingerprint = str(row_payload["fingerprint"])
        if row_payload.get(
            "emitted_error_fingerprint"
        ) == fingerprint and not self.event_repeat_due(row_payload):
            return True
        source_id = str(row_payload["source_id"])
        context = {
            "id": source_id,
            "source_id": source_id,
            "target_name": str(row_payload["target_name"]),
            "run_id": run_id_for(),
            "state": "failed",
        }
        if not self.emit_issue(
            context=context,
            error=str(row_payload["message"]),
            component="target_preflight",
            severity="warning",
        ):
            return False
        now_text = event_timestamp()
        with self.store.connect() as conn:
            conn.execute(
                """
                UPDATE target_preflight_failures
                SET emitted_error_fingerprint = ?, emitted_error_at = ?, updated_at = ?
                WHERE source_id = ?
                """,
                (fingerprint, now_text, now_text, source_id),
            )
        return True

    def emit_failed_attempt(self, attempt_id: str, *, component: str = "target") -> None:
        attempt = self.store.load_attempt(attempt_id)
        message = str(attempt["last_error"] or "Jeb attempt failed")
        self.emit_attempt_issue(attempt, message=message, component=component)

    def emit_cleanup_failed(self, attempt_id: str, message: str) -> None:
        attempt = self.store.load_attempt(attempt_id)
        self.emit_attempt_issue(attempt, message=message, component="cleanup")

    def emit_attempt_canceled(self, attempt_id: str) -> None:
        attempt = self.store.load_attempt(attempt_id)
        event = cloud_event(
            source=self.config.events.source,
            type="io.riverhog.jeb.attempt.canceled",
            subject=attempt_id,
            data={
                "attempt_id": attempt_id,
                "source_id": str(attempt["source_id"]),
                "state": "canceled",
                "target": str(attempt["target_name"]),
                "run_id": str(attempt["run_id"]),
            },
        )
        self.event_log.append(event, owner="jeb")

    def emit_attempt_issue(
        self,
        attempt: Mapping[str, Any] | sqlite3.Row,
        *,
        message: str,
        component: str,
    ) -> bool:
        attempt_payload = dict(attempt)
        fingerprint = hashlib.sha256(
            f"{attempt_payload['id']}:{component}:{message}".encode()
        ).hexdigest()[:24]
        if attempt_payload.get(
            "emitted_error_fingerprint"
        ) == fingerprint and not self.event_repeat_due(attempt_payload):
            return True
        if not self.emit_issue(
            context=attempt_payload,
            error=message,
            component=component,
            severity="error",
        ):
            return False
        self.store.set_attempt_fields(
            str(attempt_payload["id"]),
            emitted_error_fingerprint=fingerprint,
            emitted_error_at=event_timestamp(),
        )
        return True

    def event_repeat_due(self, batch: Mapping[str, Any]) -> bool:
        last_sent = batch.get("emitted_error_at")
        if not last_sent:
            return True
        try:
            sent_at = parse_utc_timestamp(str(last_sent))
        except ValueError:
            return True
        return event_repeat_due(
            last_emitted_at=sent_at,
            current=current_time(),
            interval=self.config.events.repeat_interval_seconds,
            repeat_time=self.config.events.repeat_time,
            repeat_timezone=self.config.events.repeat_timezone,
        )

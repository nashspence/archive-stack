from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from riverhog_core.webhooks import (
    WebhookConfig,
    build_collection_lifecycle_payload,
    build_jeb_event_payload,
    build_munchy_job_payload,
)

NOW = datetime(2026, 7, 14, tzinfo=UTC)
CONFIG = WebhookConfig(
    url="https://example.invalid/webhook",
    base_url="https://riverhog.example.invalid",
)
CONTRACT_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "webhooks"
    / "operator-notifications.v1.json"
)


def _collection_details(event: str) -> dict[str, object]:
    common: dict[str, object] = {
        "files_total": 2,
        "files_uploaded": 2,
        "bytes_total": 10,
        "uploaded_bytes": 10,
    }
    if event == "collections.upload_staged":
        return {**common, "state": "archiving"}
    if event == "collections.finalized":
        return {
            **common,
            "archive_storage_prefix": "opaque-docs",
            "archive_objects": 3,
            "archive_store": "b2",
            "archive_total_bytes": 20,
        }
    if event == "collections.archive_retrying":
        return {
            "attempts": 2,
            "failed_at": "2026-07-14T00:00:00Z",
            "next_retry_at": "2026-07-14T00:05:00Z",
            "retry_delay_seconds": 300,
            "error": "temporary network failure",
        }
    return {
        "attempts": 1,
        "failed_at": "2026-07-14T00:00:00Z",
        "error": "manifest mismatch",
    }


def _all_operator_payloads() -> list[dict[str, object]]:
    collection_payloads = [
        build_collection_lifecycle_payload(
            config=CONFIG,
            event=event,
            collection_id="2025/20250102T030405Z__docs",
            delivered_at=NOW,
            details=_collection_details(event),
        )
        for event in (
            "collections.upload_staged",
            "collections.finalized",
            "collections.archive_retrying",
            "collections.archive_failed",
        )
    ]
    job = {
        "job_id": "job-docs-1",
        "collection_slug": "docs",
        "collection_timestamp": "20260714T000000Z",
        "phase": "working",
        "state": "running",
    }
    munchy_payloads = [
        build_munchy_job_payload(
            event=event,
            job=job,
            message="Current job status.",
            severity="warning" if event.endswith(("issue", "reminder")) else "info",
            delivered_at=NOW,
            details=(
                {
                    "upload_progress": {"files_uploaded": 1, "files_total": 2},
                    "reminder_count": 1,
                    "reminder_interval_seconds": 3600,
                }
                if event == "job.upload_waiting.reminder"
                else None
            ),
        )
        for event in (
            "job.received",
            "review.handoff",
            "archive.handoff",
            "collection_archive.handoff",
            "job.issue",
            "job.upload_waiting.reminder",
            "job.succeeded",
        )
    ]
    jeb_payload = build_jeb_event_payload(
        event="jeb.issue",
        context={
            "id": "attempt-1",
            "batch_id": "batch-1",
            "source_id": "example-camera",
            "target_name": "munchy",
            "target_type": "munchy",
            "state": "failed",
        },
        message="Routing needs attention.",
        severity="warning",
        delivered_at=NOW,
        details={"component": "routing", "error": "no route matched"},
    )
    return [*collection_payloads, *munchy_payloads, jeb_payload]


def test_every_operator_event_has_a_valid_payload_builder() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    declared = {event["event"] for event in contract["events"]}
    built = {payload["event"] for payload in _all_operator_payloads()}

    assert built == declared


def test_collection_lifecycle_payload_uses_immutable_collection_identity() -> None:
    payload = build_collection_lifecycle_payload(
        config=CONFIG,
        event="collections.finalized",
        collection_id="2025/20250102T030405Z__docs",
        delivered_at=NOW,
        details=_collection_details("collections.finalized"),
    )

    assert payload["collection_id"] == "2025/20250102T030405Z__docs"
    assert payload["type"] == "collection_lifecycle"

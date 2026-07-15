from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from riverhog_core.webhooks import (
    WebhookConfig,
    build_archive_restore_canceled_payload,
    build_archive_restore_completed_payload,
    build_archive_restore_failed_payload,
    build_archive_restore_ready_payload,
    build_archive_restore_retrying_payload,
    build_archive_restore_started_payload,
    build_collection_lifecycle_payload,
    build_jeb_event_payload,
    build_munchy_job_payload,
)

NOW = datetime(2026, 7, 14, tzinfo=UTC)
CONFIG = WebhookConfig(
    url="https://example.invalid/webhook",
    base_url="https://riverhog.example.invalid",
)
COLLECTIONS = [{"collection_id": "2025/20250102T030405Z__docs"}]
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
            "archive_object_path": "archive/docs.tar.age",
            "archive_total_bytes": 20,
            "archive_sha256": "a" * 64,
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
    archive_payloads = [
        build_archive_restore_started_payload(
            config=CONFIG,
            restore_id="ar-docs-1",
            retrieval_tier="bulk",
            estimated_ready_at="2026-07-16T00:00:00Z",
            collections=COLLECTIONS,
            delivered_at=NOW,
        ),
        build_archive_restore_ready_payload(
            config=CONFIG,
            restore_id="ar-docs-1",
            expires_at="2026-07-17T00:00:00Z",
            collections=COLLECTIONS,
            delivered_at=NOW,
        ),
        build_archive_restore_completed_payload(
            config=CONFIG,
            restore_id="ar-docs-1",
            collections=COLLECTIONS,
            delivered_at=NOW,
        ),
        build_archive_restore_retrying_payload(
            config=CONFIG,
            restore_id="ar-docs-1",
            collections=COLLECTIONS,
            delivered_at=NOW,
            attempts=2,
            failed_at="2026-07-14T00:00:00Z",
            next_retry_at="2026-07-14T00:05:00Z",
            retry_delay_seconds=300,
            error="temporary network failure",
        ),
        build_archive_restore_failed_payload(
            config=CONFIG,
            restore_id="ar-docs-1",
            collections=COLLECTIONS,
            delivered_at=NOW,
            attempts=1,
            failed_at="2026-07-14T00:00:00Z",
            error="manifest mismatch",
        ),
        build_archive_restore_canceled_payload(
            config=CONFIG,
            restore_id="ar-docs-1",
            collections=COLLECTIONS,
            delivered_at=NOW,
        ),
    ]
    collection_events = (
        "collections.upload_staged",
        "collections.finalized",
        "collections.archive_retrying",
        "collections.archive_failed",
    )
    collection_payloads = [
        build_collection_lifecycle_payload(
            config=CONFIG,
            event=event,
            collection_id="2025/20250102T030405Z__docs",
            delivered_at=NOW,
            details=_collection_details(event),
        )
        for event in collection_events
    ]
    job = {
        "job_id": "job-docs-1",
        "collection_slug": "docs",
        "collection_timestamp": "20260714T000000Z",
        "phase": "working",
        "state": "running",
    }
    munchy_events = (
        "job.received",
        "review.handoff",
        "archive.handoff",
        "collection_archive.handoff",
        "job.issue",
        "job.upload_waiting.reminder",
        "job.succeeded",
    )
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
        for event in munchy_events
    ]
    jeb_payload = build_jeb_event_payload(
        event="jeb.issue",
        context={
            "id": "attempt-1",
            "batch_id": "batch-1",
            "account_id": "example-camera",
            "target_name": "munchy",
            "target_type": "munchy",
            "state": "failed",
        },
        message="Routing needs attention.",
        severity="warning",
        delivered_at=NOW,
        details={"component": "routing", "error": "no route matched"},
    )
    return [*archive_payloads, *collection_payloads, *munchy_payloads, jeb_payload]


def test_every_operator_event_has_a_valid_payload_builder() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    declared = {event["event"] for event in contract["events"]}
    built = {payload["event"] for payload in _all_operator_payloads()}

    assert built == declared


def test_operator_contract_contains_only_runtime_enforced_fields() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    assert set(contract) == {"schema_version", "surface", "payload", "receiver_rendering", "events"}
    event_fields = {
        "event",
        "operator_urgency",
        "operator_action",
        "operator_action_by_component",
        "canonical_notification",
        "required_payload_fields",
        "type_values",
    }
    assert all(set(event) <= event_fields for event in contract["events"])


def test_archive_restore_started_payload_names_retrieval_and_collection() -> None:
    payload = build_archive_restore_started_payload(
        config=CONFIG,
        restore_id="ar-docs-1",
        retrieval_tier="bulk",
        estimated_ready_at="2026-07-16T00:00:00Z",
        collections=COLLECTIONS,
        delivered_at=NOW,
    )

    assert payload["event"] == "archive_restore.started"
    assert payload["type"] == "archive_restore"
    assert payload["retrieval_tier"] == "bulk"
    assert payload["collections"] == [
        {
            "collection_id": "2025/20250102T030405Z__docs",
            "collection_url": "https://riverhog.example.invalid/v1/collections/2025/20250102T030405Z__docs",
        }
    ]


def test_archive_restore_ready_payload_describes_automatic_materialization() -> None:
    payload = build_archive_restore_ready_payload(
        config=CONFIG,
        restore_id="ar-docs-1",
        expires_at="2026-07-17T00:00:00Z",
        collections=COLLECTIONS,
        delivered_at=NOW,
    )
    assert payload["operator_action"] == "wait for automatic materialization"


def test_archive_restore_completion_requires_no_operator_action() -> None:
    payload = build_archive_restore_completed_payload(
        config=CONFIG,
        restore_id="ar-docs-1",
        collections=COLLECTIONS,
        delivered_at=NOW,
    )
    assert payload["operator_action"] == "none"
    assert "hot storage" in str(payload["operator_message"])


def test_archive_restore_failure_is_actionable() -> None:
    payload = build_archive_restore_failed_payload(
        config=CONFIG,
        restore_id="ar-docs-1",
        collections=COLLECTIONS,
        delivered_at=NOW,
        attempts=1,
        failed_at="2026-07-14T00:00:00Z",
        error="manifest mismatch",
    )
    assert payload["event"] == "archive_restore.failed"
    assert "archive retrieval logs" in str(payload["operator_action"])


def test_collection_lifecycle_payload_uses_collection_identity() -> None:
    payload = build_collection_lifecycle_payload(
        config=CONFIG,
        event="collections.finalized",
        collection_id="2025/20250102T030405Z__docs",
        delivered_at=NOW,
        details=_collection_details("collections.finalized"),
    )
    assert payload["collection_id"] == "2025/20250102T030405Z__docs"
    assert payload["type"] == "collection_lifecycle"

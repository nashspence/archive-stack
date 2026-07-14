from __future__ import annotations

from datetime import UTC, datetime

from riverhog_core.webhooks import (
    WebhookConfig,
    build_archive_restore_completed_payload,
    build_archive_restore_failed_payload,
    build_archive_restore_ready_payload,
    build_archive_restore_started_payload,
    build_collection_lifecycle_payload,
)

NOW = datetime(2026, 7, 14, tzinfo=UTC)
CONFIG = WebhookConfig(
    url="https://example.invalid/webhook",
    base_url="https://riverhog.example.invalid",
)
COLLECTIONS = [{"collection_id": "docs"}]


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
            "collection_id": "docs",
            "collection_url": "https://riverhog.example.invalid/v1/collections/docs",
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
        collection_id="docs",
        delivered_at=NOW,
    )
    assert payload["collection_id"] == "docs"
    assert payload["type"] == "collection_lifecycle"

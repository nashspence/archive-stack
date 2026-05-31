from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from riverhog_core.webhooks import (
    ImagesReadyBatch,
    ReadyImage,
    WebhookConfig,
    build_collection_lifecycle_payload,
    build_copy_label_needed_payload,
    build_fetch_waiting_payload,
    build_images_ready_payload,
    build_recovery_completed_payload,
    build_recovery_ready_payload,
    build_recovery_started_payload,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "webhooks"
    / "operator-notifications.v1.json"
)


def test_operator_webhook_contract_covers_current_events() -> None:
    contract = json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))
    events = {event["event"]: event for event in contract["events"]}

    assert contract["surface"] == "riverhog.operator_webhook"
    assert contract["receiver_rendering"]["title"] == "payload.notification.title"
    assert contract["receiver_rendering"]["message"] == "payload.notification.body"
    assert contract["receiver_rendering"]["actors"] == {
        "riverhog": "🐷",
        "djdan": "👨🏻‍🎤",
    }
    assert set(events) == {
        "collections.upload_staged",
        "collections.finalized",
        "collections.archive_retrying",
        "collections.planner_failed",
        "images.ready",
        "images.ready.reminder",
        "images.copy_label_needed",
        "fetches.waiting_media",
        "fetches.waiting_media.reminder",
        "glacier_recovery.started",
        "glacier_recovery.ready",
        "glacier_recovery.ready.reminder",
        "glacier_recovery.completed",
    }
    for event in events.values():
        templates = event.get("canonical_notification_by_type") or {
            "default": event.get("canonical_notification")
        }
        assert all(
            template["title_template"] == "{emoji} {subject_40}"
            for template in templates.values()
        )
        assert all(len(template["body_template"]) <= 150 for template in templates.values())
    assert events["collections.planner_failed"]["operator_urgency"] == "critical"
    assert events["fetches.waiting_media"]["delivery"]["mode"] == "durable"
    assert events["glacier_recovery.started"]["operator_urgency"] == "time_sensitive"


def test_build_images_ready_payload_supports_multiple_images() -> None:
    payload = build_images_ready_payload(
        config=WebhookConfig(url="https://example.test/hook", base_url="https://api.test"),
        batch=ImagesReadyBatch(
            batch_id="batch-1",
            images=[
                ReadyImage(
                    image_id="20260420T040001Z", filename="20260420T040001Z.iso", iso_available=True
                ),
                ReadyImage(
                    image_id="20260420T040002Z", filename="20260420T040002Z.iso", iso_available=True
                ),
            ],
        ),
        delivered_at=datetime(2026, 4, 20, tzinfo=UTC),
    )
    assert payload["event"] == "images.ready"
    assert payload["operator_urgency"] == "time_sensitive"
    assert payload["operator_action"] == "run `djdan burn`"
    assert len(payload["images"]) == 2
    assert payload["images"][0]["download_url"].endswith("/v1/images/20260420T040001Z/iso")
    assert payload["notification"] == {
        "title": "👨🏻‍🎤 20260420T040001Z.iso +1",
        "body": (
            "The pigs got some discs ready to burn, dawg! Run `djdan burn` so we can "
            "get spinnin'."
        ),
    }


def test_build_recovery_ready_payload_includes_session_and_image_urls() -> None:
    payload = build_recovery_ready_payload(
        config=WebhookConfig(url="https://example.test/hook", base_url="https://api.test"),
        session_id="rs-20260420T040001Z-1",
        restore_expires_at="2026-04-20T06:00:00Z",
        images=[
            {
                "image_id": "20260420T040001Z",
                "filename": "20260420T040001Z.iso",
            }
        ],
        delivered_at=datetime(2026, 4, 20, 5, 0, tzinfo=UTC),
        reminder_count=0,
        reminder=False,
    )
    assert payload == {
        "event": "glacier_recovery.ready",
        "type": "image_rebuild",
        "session_id": "rs-20260420T040001Z-1",
        "session_url": "https://api.test/v1/recovery-sessions/rs-20260420T040001Z-1",
        "delivered_at": "2026-04-20T05:00:00Z",
        "restore_expires_at": "2026-04-20T06:00:00Z",
        "reminder_count": 0,
        "reminder_interval_seconds": 3600.0,
        "operator_urgency": "time_sensitive",
        "operator_action": "Rebuild and burn replacement media before the restore expires",
        "operator_message": (
            "Glacier recovery data is ready for replacement media. Complete the "
            "rebuild and burn workflow before the temporary restore window expires."
        ),
        "images": [
            {
                "image_id": "20260420T040001Z",
                "filename": "20260420T040001Z.iso",
                "image_url": "https://api.test/v1/images/20260420T040001Z",
            }
        ],
        "collections": [],
        "notification": {
            "title": "👨🏻‍🎤 20260420T040001Z.iso",
            "body": (
                "Glacier data is cued up! Run `djdan burn` before the restore window "
                "closes."
            ),
        },
    }


def test_build_recovery_lifecycle_payloads_are_explicit_about_glacier_work() -> None:
    config = WebhookConfig(url="https://example.test/hook", base_url="https://api.test")
    started = build_recovery_started_payload(
        config=config,
        session_id="rs-docs-restore-1",
        recovery_type="collection_restore",
        retrieval_tier="bulk",
        estimated_ready_at="2026-04-22T05:00:00Z",
        images=[],
        collections=[{"collection_id": "docs"}],
        delivered_at=datetime(2026, 4, 20, 5, 0, tzinfo=UTC),
    )
    completed = build_recovery_completed_payload(
        config=config,
        session_id="rs-docs-restore-1",
        recovery_type="collection_restore",
        images=[],
        collections=[{"collection_id": "docs"}],
        delivered_at=datetime(2026, 4, 22, 5, 0, tzinfo=UTC),
    )

    assert started["event"] == "glacier_recovery.started"
    assert started["operator_urgency"] == "time_sensitive"
    assert "long time" in str(started["operator_message"])
    assert started["notification"] == {
        "title": "🐷 docs",
        "body": (
            "Oink, Glacier recovery is underway. This may take a long while; the archived "
            "data is safe."
        ),
    }
    assert started["collections"] == [
        {
            "collection_id": "docs",
            "collection_url": "https://api.test/v1/collections/docs",
        }
    ]
    assert completed["event"] == "glacier_recovery.completed"
    assert completed["operator_action"] == "No operator action required"
    assert completed["notification"] == {
        "title": "🐷 docs",
        "body": "Oink, Glacier recovery is done, and the missing pinned files are hot again.",
    }


def test_build_fetch_waiting_payload_names_operator_action() -> None:
    payload = build_fetch_waiting_payload(
        config=WebhookConfig(url="https://example.test/hook", base_url="https://api.test"),
        fetch_id="fx-1",
        target="docs/tax/2022/invoice-123.pdf",
        files=1,
        bytes=1234,
        copies=[
            {
                "copy_id": "20260530T000000Z-1",
                "volume_id": "20260530T000000Z",
                "location": "red binder",
            }
        ],
        delivered_at=datetime(2026, 5, 31, 12, 0, tzinfo=UTC),
        reminder_count=0,
        reminder=False,
    )

    assert payload["event"] == "fetches.waiting_media"
    assert payload["operator_action"] == "Run `djdan fetch fx-1`"
    assert payload["manifest_url"] == "https://api.test/v1/fetches/fx-1/manifest"
    assert payload["notification"] == {
        "title": "👨🏻‍🎤 invoice-123.pdf",
        "body": (
            "Need that disc read, friend! Run `djdan fetch` so I can get those files hot again."
        ),
    }


def test_build_collection_lifecycle_payload_includes_links_and_details() -> None:
    payload = build_collection_lifecycle_payload(
        config=WebhookConfig(url="https://example.test/hook", base_url="https://api.test"),
        event="collections.upload_staged",
        collection_id="2025/20250712T213200Z__home-videos",
        delivered_at=datetime(2026, 5, 25, 18, 0, tzinfo=UTC),
        details={
            "files_total": 572,
            "files_uploaded": 572,
            "bytes_total": 73763193518,
        },
    )

    assert payload["event"] == "collections.upload_staged"
    assert payload["type"] == "collection_lifecycle"
    assert payload["collection_id"] == "2025/20250712T213200Z__home-videos"
    assert payload["operator_urgency"] == "time_sensitive"
    assert payload["operator_action"] == "none"
    assert payload["collection_url"].endswith("/v1/collections/2025/20250712T213200Z__home-videos")
    assert payload["files_total"] == 572
    assert payload["notification"] == {
        "title": "🐷 home-videos",
        "body": "Oink oink, this upload is safely staged; Glacier archiving is underway.",
    }


def test_build_copy_label_needed_payload_includes_label_and_image_url() -> None:
    payload = build_copy_label_needed_payload(
        config=WebhookConfig(url="https://example.test/hook", base_url="https://api.test"),
        image_id="20260526T204059Z",
        copy_id="20260526T204059Z-1",
        label_text="20260526T204059Z-1",
        delivered_at=datetime(2026, 5, 26, 21, 15, tzinfo=UTC),
    )

    assert payload == {
        "event": "images.copy_label_needed",
        "type": "copy_lifecycle",
        "image_id": "20260526T204059Z",
        "copy_id": "20260526T204059Z-1",
        "label_text": "20260526T204059Z-1",
        "delivered_at": "2026-05-26T21:15:00Z",
        "operator_urgency": "time_sensitive",
        "operator_action": "label the physical disc exactly as label_text",
        "image_url": "https://api.test/v1/images/20260526T204059Z",
        "notification": {
            "title": "👨🏻‍🎤 20260526T204059Z-1",
            "body": (
                "That burn verified clean! Label the disc exactly, then tell me where it lives."
            ),
        },
    }

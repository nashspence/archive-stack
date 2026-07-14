from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from riverhog_core.webhooks import (
    ImagesReadyBatch,
    ReadyImage,
    WebhookConfig,
    _canonical_notification_from_contract,
    build_archive_restore_canceled_payload,
    build_archive_restore_completed_payload,
    build_archive_restore_failed_payload,
    build_archive_restore_paused_reminder_payload,
    build_archive_restore_ready_payload,
    build_archive_restore_retrying_payload,
    build_archive_restore_started_payload,
    build_collection_lifecycle_payload,
    build_disc_label_needed_payload,
    build_fetch_queued_payload,
    build_images_ready_payload,
    build_jeb_event_payload,
    build_munchy_job_payload,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "webhooks"
    / "operator-notifications.v1.json"
)

_JEB_METADATA_PROJECTION_SUMMARY = (
    "Missing GPS metadata. Next: fix metadata projection config, then retry Jeb archive."
)


def _representative_notification_values() -> dict[str, str]:
    long_error = (
        "runner job did not succeed: remote upload complete but encode failed with a long "
        "diagnostic containing paths, byte counts, progress counters, and nested service state"
    )
    return {
        "error": long_error,
        "message": long_error,
        "notification_summary": (
            "Concise operator summary. Next: inspect details, fix the issue, then retry."
        ),
        "next_retry_at": "2026-06-30T14:00:00-07:00",
    }


def test_operator_webhook_contract_covers_current_events() -> None:
    contract = json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))
    events = {event["event"]: event for event in contract["events"]}

    assert contract["surface"] == "riverhog.operator_webhook"
    assert contract["receiver_rendering"]["title"] == "payload.notification.title"
    assert contract["receiver_rendering"]["message"] == "payload.notification.body"
    assert contract["receiver_rendering"]["actors"] == {
        "riverhog": "🐷",
        "djdan": "👨🏻‍🎤",
        "munchy": "🤤",
        "jeb": "🤖",
    }
    assert contract["endpoint_config"]["reminder_time_env"] == (
        "RIVERHOG_OPERATOR_WEBHOOK_REMINDER_TIME"
    )
    assert contract["endpoint_config"]["reminder_timezone_env"] == (
        "RIVERHOG_OPERATOR_WEBHOOK_REMINDER_TIMEZONE"
    )
    assert "failure_notification_interval_env" not in contract["endpoint_config"]
    assert set(events) == {
        "job.received",
        "review.handoff",
        "collection_archive.handoff",
        "archive.handoff",
        "job.issue",
        "job.upload_waiting.reminder",
        "job.succeeded",
        "jeb.issue",
        "collections.upload_staged",
        "collections.finalized",
        "collections.archive_retrying",
        "collections.archive_failed",
        "collections.planner_failed",
        "images.ready",
        "images.ready.reminder",
        "images.disc_label_needed",
        "fetches.queued_djdan",
        "fetches.queued_djdan.reminder",
        "archive_restore.started",
        "archive_restore.ready",
        "archive_restore.ready.reminder",
        "archive_restore.completed",
        "archive_restore.retrying",
        "archive_restore.failed",
        "archive_restore.canceled",
        "archive_restore.paused.reminder",
    }
    for event in events.values():
        templates = event.get("canonical_notification_by_type") or {
            "default": event.get("canonical_notification")
        }
        assert all(
            template["title_template"] == "{emoji} {subject_40}" for template in templates.values()
        )
        assert all(len(template["body_template"]) <= 150 for template in templates.values())
    assert events["collections.planner_failed"]["operator_urgency"] == "time_sensitive"
    assert events["collections.archive_failed"]["operator_urgency"] == "critical"
    assert events["job.issue"]["canonical_notification"]["actor"] == "munchy"
    assert events["jeb.issue"]["canonical_notification"]["actor"] == "jeb"
    assert events["jeb.issue"]["operator_urgency"] == "time_sensitive"
    assert events["jeb.issue"]["delivery"]["reminder"] is True
    assert (
        events["jeb.issue"]["delivery"]["reminder_interval_env"]
        == "RIVERHOG_OPERATOR_WEBHOOK_REMINDER_INTERVAL"
    )
    assert events["job.upload_waiting.reminder"]["operator_urgency"] == "time_sensitive"
    assert events["job.upload_waiting.reminder"]["delivery"]["reminder"] is True
    assert (
        events["collections.archive_retrying"]["delivery"]["reminder_interval_env"]
        == "RIVERHOG_OPERATOR_WEBHOOK_REMINDER_INTERVAL"
    )
    assert (
        events["archive_restore.paused.reminder"]["delivery"]["reminder_time_env"]
        == "RIVERHOG_OPERATOR_WEBHOOK_REMINDER_TIME"
    )
    assert events["fetches.queued_djdan"]["delivery"]["mode"] == "durable"
    assert events["archive_restore.started"]["operator_urgency"] == "time_sensitive"
    assert events["archive_restore.failed"]["operator_urgency"] == "time_sensitive"
    assert events["archive_restore.paused.reminder"]["delivery"]["reminder"] is True


def test_operator_webhook_contract_rendered_notifications_fit_push_limits() -> None:
    contract = json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))
    title_limit = contract["receiver_rendering"]["title_max_chars"]
    body_limit = contract["receiver_rendering"]["body_max_chars"]
    values = _representative_notification_values()

    for event in contract["events"]:
        templates = event.get("canonical_notification_by_type")
        notification_types = list(templates) if isinstance(templates, dict) else [None]
        for notification_type in notification_types:
            notification = _canonical_notification_from_contract(
                event=event["event"],
                notification_type=notification_type,
                subject=("very-long-subject-name-that-should-never-overflow-phone-push-title"),
                values=values,
            )

            assert len(notification["title"]) <= title_limit, event["event"]
            assert len(notification["body"]) <= body_limit, event["event"]
            assert "{" not in notification["title"], event["event"]
            assert "{" not in notification["body"], event["event"]


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
            "The pigs got some discs ready to burn, dawg! Run `djdan burn` so we can get spinnin'."
        ),
    }


def test_build_recovery_ready_payload_includes_session_and_image_urls() -> None:
    payload = build_archive_restore_ready_payload(
        config=WebhookConfig(url="https://example.test/hook", base_url="https://api.test"),
        restore_id="ar-20260420T040001Z-1",
        expires_at="2026-04-20T06:00:00Z",
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
        "event": "archive_restore.ready",
        "type": "disc_rebuild",
        "restore_id": "ar-20260420T040001Z-1",
        "restore_url": "https://api.test/v1/archive-restores/ar-20260420T040001Z-1",
        "delivered_at": "2026-04-20T05:00:00Z",
        "expires_at": "2026-04-20T06:00:00Z",
        "reminder_count": 0,
        "reminder_interval_seconds": 3600.0,
        "operator_urgency": "time_sensitive",
        "operator_action": "Rebuild and burn replacement media before the restore expires",
        "operator_message": (
            "Archive restore data is ready for replacement media. Complete the "
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
            "body": ("Archived data is ready! Run `djdan burn` before the restore window closes."),
        },
    }


def test_build_recovery_lifecycle_payloads_are_explicit_about_archive_work() -> None:
    config = WebhookConfig(url="https://example.test/hook", base_url="https://api.test")
    started = build_archive_restore_started_payload(
        config=config,
        restore_id="ar-docs-restore-1",
        restore_type="fetch_materialization",
        retrieval_tier="bulk",
        estimated_ready_at="2026-04-22T05:00:00Z",
        images=[],
        collections=[{"collection_id": "docs"}],
        delivered_at=datetime(2026, 4, 20, 5, 0, tzinfo=UTC),
    )
    completed = build_archive_restore_completed_payload(
        config=config,
        restore_id="ar-docs-restore-1",
        restore_type="fetch_materialization",
        images=[],
        collections=[{"collection_id": "docs"}],
        delivered_at=datetime(2026, 4, 22, 5, 0, tzinfo=UTC),
    )

    assert started["event"] == "archive_restore.started"
    assert started["operator_urgency"] == "time_sensitive"
    assert "long time" in str(started["operator_message"])
    assert started["notification"] == {
        "title": "🐷 docs",
        "body": (
            "Oink, an archive restore is underway. This may take a long while; the archived "
            "data is safe."
        ),
    }
    assert started["collections"] == [
        {
            "collection_id": "docs",
            "collection_url": "https://api.test/v1/collections/docs",
        }
    ]
    assert completed["event"] == "archive_restore.completed"
    assert completed["operator_action"] == "No operator action required"
    assert completed["notification"] == {
        "title": "🐷 docs",
        "body": (
            "Oink, fetch materialization is complete, and the missing fetch files are hot again."
        ),
    }


def test_build_recovery_retry_failure_cancel_and_pause_payloads() -> None:
    config = WebhookConfig(url="https://example.test/hook", base_url="https://api.test")
    images = [{"image_id": "20260420T040001Z", "filename": "20260420T040001Z.iso"}]
    collections = [{"collection_id": "docs"}]
    delivered_at = datetime(2026, 4, 20, 5, 0, tzinfo=UTC)

    retrying = build_archive_restore_retrying_payload(
        config=config,
        restore_id="ar-20260420T040001Z-rebuild-1",
        restore_type="disc_rebuild",
        images=images,
        collections=collections,
        delivered_at=delivered_at,
        attempts=2,
        failed_at="2026-04-20T05:00:00Z",
        next_retry_at="2026-04-20T05:05:00Z",
        retry_delay_seconds=300.0,
        error="S3 restore request timed out",
    )
    failed = build_archive_restore_failed_payload(
        config=config,
        restore_id="ar-docs-restore-1",
        restore_type="fetch_materialization",
        images=[],
        collections=collections,
        delivered_at=delivered_at,
        attempts=1,
        failed_at="2026-04-20T05:00:00Z",
        error="collection archive member sha256 mismatch",
    )
    canceled = build_archive_restore_canceled_payload(
        config=config,
        restore_id="ar-docs-restore-1",
        restore_type="fetch_materialization",
        images=[],
        collections=collections,
        delivered_at=delivered_at,
    )
    paused = build_archive_restore_paused_reminder_payload(
        config=config,
        restore_id="ar-20260420T040001Z-rebuild-1",
        images=images,
        collections=collections,
        delivered_at=delivered_at,
        reminder_count=3,
        reminder_interval_seconds=86400.0,
    )

    assert retrying["event"] == "archive_restore.retrying"
    assert retrying["operator_action"] == (
        "wait unless failures persist beyond normal connectivity trouble"
    )
    assert "Next retry: 2026-04-20T05:05:00Z" in retrying["notification"]["body"]
    assert failed["event"] == "archive_restore.failed"
    assert failed["operator_urgency"] == "time_sensitive"
    assert "will not retry" in failed["notification"]["body"]
    assert canceled["event"] == "archive_restore.canceled"
    assert canceled["operator_urgency"] == "passive"
    assert paused["event"] == "archive_restore.paused.reminder"
    assert paused["reminder_count"] == 4
    assert paused["reminder_interval_seconds"] == 86400.0
    assert paused["operator_action"] == (
        "Run `djdan disc rebuild resume ar-20260420T040001Z-rebuild-1` when ready"
    )


def test_build_fetch_queued_payload_names_operator_action() -> None:
    payload = build_fetch_queued_payload(
        config=WebhookConfig(url="https://example.test/hook", base_url="https://api.test"),
        fetch_id="fx-1",
        name="Recover invoice",
        files=1,
        bytes=1234,
        discs=[
            {
                "disc_id": "20260530T000000Z-1",
                "image_id": "20260530T000000Z",
                "location": "red binder",
            }
        ],
        delivered_at=datetime(2026, 5, 31, 12, 0, tzinfo=UTC),
        reminder_count=0,
        reminder=False,
    )

    assert payload["event"] == "fetches.queued_djdan"
    assert payload["operator_action"] == "Run `djdan fetch fx-1`"
    assert payload["manifest_url"] == "https://api.test/v1/fetches/fx-1/manifest"
    assert payload["notification"] == {
        "title": "👨🏻‍🎤 Recover invoice",
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
        "body": "Oink oink, this upload is safely staged; archiving is underway.",
    }


def test_build_archive_retrying_payload_names_error_and_next_retry() -> None:
    payload = build_collection_lifecycle_payload(
        config=WebhookConfig(url="https://example.test/hook", base_url="https://api.test"),
        event="collections.archive_retrying",
        collection_id="2025/20250722T012216Z__gopro-10-5-2023-to-5-12-2024",
        delivered_at=datetime(2026, 6, 2, 9, 0, tzinfo=UTC),
        details={
            "attempts": 3,
            "failed_at": "2026-06-02T09:00:00Z",
            "next_retry_at": "2026-06-02T09:05:00Z",
            "retry_delay_seconds": 300.0,
            "error": "S3 upload timed out while sending part 12",
        },
    )

    assert payload["event"] == "collections.archive_retrying"
    assert payload["error"] == "S3 upload timed out while sending part 12"
    assert payload["notification"] == {
        "title": "🐷 gopro-10-5-2023-to-5-12-2024",
        "body": (
            "Oink, archive finalizing hit a retryable issue: S3 upload timed out "
            "while sending part 12. Next retry: 2026-06-02T09:05:00Z; no action yet."
        ),
    }


def test_build_archive_failed_payload_is_critical_and_nonretrying() -> None:
    payload = build_collection_lifecycle_payload(
        config=WebhookConfig(url="https://example.test/hook", base_url="https://api.test"),
        event="collections.archive_failed",
        collection_id="2025/20250722T012216Z__gopro-10-5-2023-to-5-12-2024",
        delivered_at=datetime(2026, 6, 2, 9, 0, tzinfo=UTC),
        details={
            "attempts": 25,
            "failed_at": "2026-06-02T09:00:00Z",
            "error": "collection archive member sha256 mismatch: 20240512T093848Z.MP4",
        },
    )

    assert payload["event"] == "collections.archive_failed"
    assert payload["operator_urgency"] == "critical"
    assert payload["operator_action"] == "inspect Riverhog archive logs and collection upload state"
    assert payload["notification"] == {
        "title": "🐷 gopro-10-5-2023-to-5-12-2024",
        "body": (
            "Oink! Archiving stopped: collection archive member sha256 "
            "mismatch: 20240512T093848Z.MP4. Please inspect Riverhog; I will not retry this."
        ),
    }


def test_build_munchy_job_payload_uses_operator_notification_contract() -> None:
    payload = build_munchy_job_payload(
        event="job.issue",
        job={
            "job_id": "job-1",
            "collection_slug": "camera-collection-archive-q49",
            "collection_timestamp": "20260606T120000Z",
            "phase": "preflight_failed",
            "state": "failed",
        },
        message="Local media preflight failed.",
        severity="critical",
        delivered_at=datetime(2026, 6, 6, 12, 0, tzinfo=UTC),
        recipient="operator",
        details={
            "component": "preflight",
            "error": "atom extends past EOF (bad.mp4)",
            "failed_file_count": 1,
        },
    )

    assert payload["event"] == "job.issue"
    assert payload["type"] == "munchy_job"
    assert payload["source"] == "munchy"
    assert payload["actor"] == "munchy"
    assert payload["delivered_at"] == "2026-06-06T12:00:00Z"
    assert payload["operator_urgency"] == "time_sensitive"
    assert payload["operator_action"] == "inspect Munchy job details when convenient"
    assert payload["component"] == "preflight"
    assert payload["notification"] == {
        "title": "🤤 camera-collection-archive-q49",
        "body": "atom extends past EOF (bad.mp4)",
    }


def test_build_munchy_upload_waiting_reminder_payload() -> None:
    payload = build_munchy_job_payload(
        event="job.upload_waiting.reminder",
        job={
            "job_id": "job-1",
            "collection_slug": "camera-collection-archive-q49",
            "collection_timestamp": "20260606T120000Z",
            "phase": "waiting_for_eager_files:3031/5006",
            "state": "running",
        },
        message="Upload paused: 3031/5006 files. Resume or cancel.",
        severity="warning",
        delivered_at=datetime(2026, 6, 6, 12, 0, tzinfo=UTC),
        recipient="operator",
        details={
            "upload_progress": {"files_uploaded": 3031, "files_total": 5006},
            "reminder_count": 1,
            "reminder_interval_seconds": 86400,
        },
    )

    assert payload["event"] == "job.upload_waiting.reminder"
    assert payload["operator_urgency"] == "time_sensitive"
    assert payload["operator_action"] == "resume upload or cancel the Munchy job"
    assert payload["severity"] == "warning"
    assert payload["reminder_count"] == 1
    assert payload["notification"] == {
        "title": "🤤 camera-collection-archive-q49",
        "body": "Upload paused: 3031/5006 files. Resume or cancel.",
    }


def test_build_jeb_issue_payload_uses_robot_actor_and_concise_error() -> None:
    payload = build_jeb_event_payload(
        event="jeb.issue",
        context={
            "id": "20260615T120000Z__camera__abc123",
            "account_id": "camera",
            "target_name": "munchy",
            "target_type": "munchy",
            "collection_slug": "camera-archive",
            "collection_timestamp": "20260615T120000Z",
            "state": "failed",
        },
        message="cleanup failed: permission denied",
        severity="critical",
        delivered_at=datetime(2026, 6, 15, 12, 0, tzinfo=UTC),
        recipient="operator",
        details={"component": "cleanup", "error": "permission denied"},
    )

    assert payload["event"] == "jeb.issue"
    assert payload["type"] == "jeb_issue"
    assert payload["source"] == "jeb"
    assert payload["actor"] == "jeb"
    assert payload["operator_urgency"] == "time_sensitive"
    assert payload["operator_action"] == "inspect Jeb issue details"
    assert payload["recipient"] == "operator"
    assert payload["notification"] == {
        "title": "🤖 camera",
        "body": "permission denied",
    }


def test_build_jeb_target_failure_payload_keeps_push_body_concise() -> None:
    long_error = (
        "runner job did not succeed:\n"
        "- job: jeb-weekly-device-artifacts-20260630t002208z-540b5f0797db-job\n"
        "- status: job: failed | waiting_for_eager_files:0/0 | remote upload 80/80 "
        "files, 934.33 MiB / 934.33 MiB, 100.00% | remote encode 0/48 files, "
        "0.00% files, 0 B / 536.86 MiB input, 0.00% input\n"
        "- error: metadata projection failed for closet-camera/Closet_00.mp4: "
        "metadata projection requires valid GPS coordinates; set "
        "metadata_projection.allow_missing_gps=true to permit this source"
    )

    payload = build_jeb_event_payload(
        event="jeb.issue",
        context={
            "id": "20260630T002208Z__weekly-device-artifacts__540b5f0797db",
            "account_id": "closet-camera",
            "target_name": "munchy",
            "target_type": "munchy",
            "collection_slug": "weekly-device-artifacts",
            "collection_timestamp": "20260630T002208Z",
            "state": "failed",
        },
        message=long_error,
        severity="critical",
        delivered_at=datetime(2026, 6, 30, 0, 24, tzinfo=UTC),
        details={"component": "target", "error": long_error},
    )

    assert payload["message"] == _JEB_METADATA_PROJECTION_SUMMARY
    assert payload["detailed_message"] == long_error
    assert payload["error"] == long_error
    assert payload["notification"] == {
        "title": "🤖 closet-camera",
        "body": _JEB_METADATA_PROJECTION_SUMMARY,
    }
    assert len(str(payload["notification"]["body"])) <= 120


def test_build_jeb_routing_issue_payload_is_device_titled_and_actionable() -> None:
    payload = build_jeb_event_payload(
        event="jeb.issue",
        context={
            "id": "routing-preflight__nash-iphone-se2",
            "account_id": "nash-iphone-se2",
            "target_name": "munchy",
            "target_type": "munchy",
            "collection_slug": "weekly-device-artifacts",
            "collection_timestamp": "20260624T000000Z",
            "state": "failed",
        },
        message=("Munchy routing preflight failed: 2/64 files unmatched. Next: run archive-now."),
        severity="critical",
        delivered_at=datetime(2026, 6, 24, 12, 0, tzinfo=UTC),
        recipient="operator",
        details={
            "component": "routing",
            "error": (
                "Munchy routing preflight failed: 2/64 files unmatched. Next: run archive-now."
            ),
        },
    )

    assert payload["operator_urgency"] == "time_sensitive"
    assert (
        payload["operator_action"] == "fix Munchy routing, then run Jeb archive-now for the account"
    )
    assert payload["severity"] == "critical"
    assert payload["notification"] == {
        "title": "🤖 nash-iphone-se2",
        "body": "Munchy routing preflight failed: 2/64 files unmatched. Next: run archive-now.",
    }


def test_build_jeb_munchy_preflight_issue_payload_is_api_actionable() -> None:
    payload = build_jeb_event_payload(
        event="jeb.issue",
        context={
            "id": "routing-preflight__nash-iphone-se2",
            "account_id": "nash-iphone-se2",
            "target_name": "munchy",
            "target_type": "munchy",
            "collection_slug": "weekly-device-artifacts",
            "collection_timestamp": "20260624T000000Z",
            "state": "failed",
        },
        message=(
            "Munchy routing preflight API failed (HTTP 404); no upload started. "
            "Next: repair Munchy, then run archive-now."
        ),
        severity="critical",
        delivered_at=datetime(2026, 6, 24, 12, 0, tzinfo=UTC),
        recipient="operator",
        details={
            "component": "munchy_preflight",
            "error": (
                "Munchy routing preflight API failed (HTTP 404); no upload started. "
                "Next: repair Munchy, then run archive-now."
            ),
        },
    )

    assert payload["operator_urgency"] == "time_sensitive"
    assert (
        payload["operator_action"]
        == "repair Munchy routing preflight, then run Jeb archive-now for the account"
    )
    assert payload["notification"] == {
        "title": "🤖 nash-iphone-se2",
        "body": (
            "Munchy routing preflight API failed (HTTP 404); no upload started. "
            "Next: repair Munchy, then run archive-now."
        ),
    }


def test_build_disc_label_needed_payload_includes_label_and_image_url() -> None:
    payload = build_disc_label_needed_payload(
        config=WebhookConfig(url="https://example.test/hook", base_url="https://api.test"),
        image_id="20260526T204059Z",
        disc_id="20260526T204059Z-1",
        label_text="20260526T204059Z-1",
        delivered_at=datetime(2026, 5, 26, 21, 15, tzinfo=UTC),
    )

    assert payload == {
        "event": "images.disc_label_needed",
        "type": "disc_lifecycle",
        "image_id": "20260526T204059Z",
        "disc_id": "20260526T204059Z-1",
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

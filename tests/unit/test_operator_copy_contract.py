from __future__ import annotations

import json
import re
from pathlib import Path

import jsonschema

from contracts.operator import copy as operator_copy
from contracts.operator import format as operator_format

ROOT = Path(__file__).resolve().parents[2]
FEATURES_DIR = ROOT / "tests" / "acceptance" / "features"
NOTIFICATION_SCHEMA = ROOT / "contracts" / "operator" / "action-needed-notification.schema.json"


def _schema_validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(NOTIFICATION_SCHEMA.read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())


def _notification_contracts() -> list[
    tuple[operator_copy.ActionNeededNotification, dict[str, object]]
]:
    return [
        (
            operator_copy.push_burn_work_ready(
                disc_count=2,
                oldest_ready_at="2026-05-01 08:00 UTC",
            ),
            {},
        ),
        (
            operator_copy.push_disc_work_waiting_too_long(
                disc_count=2,
                oldest_ready_at="2026-05-01 08:00 UTC",
            ),
            {},
        ),
        (
            operator_copy.push_replacement_disc_needed(label_text="20260420T040001Z-3"),
            {},
        ),
        (
            operator_copy.push_recovery_approval_required(
                affected=["docs"],
                estimated_cost="12.34",
            ),
            {},
        ),
        (
            operator_copy.push_recovery_ready(
                affected=["docs"],
                expires_at="2026-05-02 08:00 UTC",
            ),
            {
                "type": "image_rebuild",
                "session_id": "rs-20260420T040001Z-rebuild-1",
                "images": [{"image_id": "20260420T040001Z"}],
            },
        ),
        (
            operator_copy.push_hot_recovery_needs_media(
                target="docs/tax/2022/invoice-123.pdf",
            ),
            {},
        ),
        (
            operator_copy.push_cloud_backup_failed(collection_id="docs", attempts=2),
            {"collection_id": "docs", "error": "s3 timeout", "attempts": 2},
        ),
        (operator_copy.push_notification_health_failed(channel="Push"), {}),
        (operator_copy.push_billing_needs_attention(reason="pricing unavailable"), {}),
        (
            operator_copy.push_setup_needs_attention(area="Storage", summary="missing bucket"),
            {},
        ),
    ]


def _acceptance_copy_references() -> set[str]:
    reference_pattern = re.compile(r'operator (?:notification )?copy "([^"]+)"')
    references: set[str] = set()
    for path in FEATURES_DIR.glob("*.feature"):
        references.update(reference_pattern.findall(path.read_text(encoding="utf-8")))
    return references


def _guided_item_text(
    item: operator_copy.GuidedItem,
    *,
    index: int = 1,
    total: int = 1,
) -> str:
    return "\n".join(
        (
            operator_copy.guided_item_header(index=index, total=total, item=item),
            operator_copy.guided_item_body(item=item),
        )
    )


def _feature_copy_text(name: str) -> str:
    match name:
        case "arc_home_no_attention":
            return operator_copy.arc_home_no_attention()
        case "arc_home_attention":
            return operator_copy.arc_home_attention(
                [
                    operator_copy.arc_item_setup_needs_attention(
                        area="Storage",
                        summary="missing bucket",
                    ),
                    operator_copy.arc_item_notification_health_failed(
                        channel="Push",
                        latest_error="delivery timeout",
                    ),
                ]
            )
        case "arc_item_cloud_backup_failed":
            return _guided_item_text(
                operator_copy.arc_item_cloud_backup_failed(
                    collection_id="docs",
                    attempts=2,
                    latest_error=None,
                )
            )
        case "arc_item_setup_needs_attention":
            return _guided_item_text(
                operator_copy.arc_item_setup_needs_attention(
                    area="Storage",
                    summary="missing bucket",
                ),
                index=2,
                total=2,
            )
        case "arc_item_notification_health_failed":
            return _guided_item_text(
                operator_copy.arc_item_notification_health_failed(
                    channel="Push",
                    latest_error="delivery timeout",
                ),
                index=1,
                total=2,
            )
        case "upload_finalized":
            return operator_copy.upload_finalized(
                collection_id="photos-2024",
                files=2,
                total_bytes=2048,
            )
        case "plan_disc_work_ready":
            return operator_copy.plan_disc_work_ready(collection_ids=["docs"], disc_count=1)
        case "images_physical_work_summary":
            return operator_copy.images_physical_work_summary(
                discs_needed=1,
                fully_protected_collections=1,
            )
        case "collection_summary":
            return operator_copy.collection_summary(
                collection_id="docs",
                cloud_backup_safe=True,
                disc_coverage="partial",
                labels=["20260420T040001Z-1"],
                storage_locations=["Shelf B1"],
            )
        case "cloud_backup_report":
            return operator_copy.cloud_backup_report(
                collection_id="docs",
                estimated_monthly_cost="0.01",
                healthy=True,
            )
        case "copy_registered":
            return operator_copy.copy_registered(
                label_text="20260420T040001Z-1",
                location="Shelf B1",
            )
        case "pin_waiting_for_disc":
            return operator_copy.pin_waiting_for_disc(
                target="docs/tax/2022/invoice-123.pdf",
                missing_bytes=None,
            )
        case "fetch_detail_pending":
            return operator_copy.fetch_detail_pending(
                target="docs/tax/2022/invoice-123.pdf",
                pending_files=1,
                partial_files=1,
            )
        case "disc_item_unfinished_local_copy":
            return _guided_item_text(
                operator_copy.disc_item_unfinished_local_copy(
                    label_text="20260420T040001Z-1"
                )
            )
        case "arc_disc_attention":
            return operator_copy.arc_disc_attention(
                [
                    operator_copy.disc_item_recovery_ready(
                        session_id="rs-20260420T040001Z-rebuild-1",
                        affected=["docs"],
                        expires_at="2026-05-02 08:00 UTC",
                    )
                ]
            )
        case "disc_item_recovery_ready":
            return _guided_item_text(
                operator_copy.disc_item_recovery_ready(
                    session_id="rs-20260420T040001Z-rebuild-1",
                    affected=["docs"],
                    expires_at="2026-05-02 08:00 UTC",
                )
            )
        case "disc_item_recovery_approval_required":
            return _guided_item_text(
                operator_copy.disc_item_recovery_approval_required(
                    session_id="rs-20260420T040001Z-rebuild-1",
                    affected=["docs"],
                    estimated_cost="12.34",
                )
            )
        case "disc_item_hot_recovery_needs_media":
            return _guided_item_text(
                operator_copy.disc_item_hot_recovery_needs_media(
                    target="docs/tax/2022/invoice-123.pdf"
                )
            )
        case "burn_backlog_cleared":
            return operator_copy.burn_backlog_cleared()
        case "burn_label_checkpoint":
            return operator_copy.burn_label_checkpoint(label_text="20260420T040001Z-1")
    raise AssertionError(f"unsupported operator copy reference: {name}")


def _base_payload(*, event: str = "operator.setup_needs_attention") -> dict[str, object]:
    return {
        "event": event,
        "title": "Setup needs attention",
        "body": "Storage: missing bucket. Run arc.",
        "urgency": "important",
    }


def test_notification_copy_payloads_match_action_needed_schema() -> None:
    validator = _schema_validator()

    for notification, metadata in _notification_contracts():
        payload = {
            **metadata,
            **notification.payload(
                delivered_at="2026-05-01T08:00:00Z",
                reminder_count=0,
            ),
        }
        validator.validate(payload)

        if notification.reminder_title or notification.reminder_body or notification.reminder_event:
            reminder_payload = {
                **metadata,
                **notification.payload(
                    reminder=True,
                    delivered_at="2026-05-01T09:00:00Z",
                    reminder_count=1,
                ),
            }
            validator.validate(reminder_payload)


def test_notification_schema_uses_public_contract_namespace() -> None:
    schema = json.loads(NOTIFICATION_SCHEMA.read_text(encoding="utf-8"))
    assert (
        schema["$id"]
        == "https://riverhog.dev/contracts/operator/action-needed-notification.schema.json"
    )


def test_notification_schema_requires_urgency() -> None:
    validator = _schema_validator()
    payload = _base_payload()
    payload.pop("urgency")

    errors = list(validator.iter_errors(payload))

    assert [error.message for error in errors] == ["'urgency' is a required property"]


def test_recovery_ready_schema_requires_event_metadata() -> None:
    validator = _schema_validator()
    payload = {
        **_base_payload(event="images.rebuild_ready"),
        "title": "Recovery is ready",
        "body": "Recovered data for docs is ready before 2026-05-02. Run arc-disc.",
        "urgency": "time-sensitive",
    }

    errors = list(validator.iter_errors(payload))

    assert sorted(error.message for error in errors) == [
        "'images' is a required property",
        "'reminder_count' is a required property",
        "'session_id' is a required property",
        "'type' is a required property",
    ]

    payload.update(
        {
            "type": "image_rebuild",
            "session_id": "rs-20260420T040001Z-rebuild-1",
            "images": [{"image_id": "20260420T040001Z"}],
            "reminder_count": 0,
        }
    )
    validator.validate(payload)


def test_cloud_backup_failure_schema_requires_event_metadata() -> None:
    validator = _schema_validator()
    payload = _base_payload(event="collections.glacier_upload.failed")

    errors = list(validator.iter_errors(payload))

    assert sorted(error.message for error in errors) == [
        "'attempts' is a required property",
        "'collection_id' is a required property",
        "'error' is a required property",
    ]

    payload.update({"collection_id": "docs", "error": "s3 timeout", "attempts": 2})
    validator.validate(payload)


def test_existing_notification_event_ids_stay_current_until_explicitly_superseded() -> None:
    assert operator_copy.push_burn_work_ready(disc_count=1).event == "images.ready"
    assert (
        operator_copy.push_burn_work_ready(disc_count=1).reminder_event
        == "images.ready.reminder"
    )
    assert (
        operator_copy.push_recovery_ready(affected=["docs"], expires_at=None).event
        == "images.rebuild_ready"
    )
    assert (
        operator_copy.push_recovery_ready(affected=["docs"], expires_at=None).reminder_event
        == "images.rebuild_ready.reminder"
    )
    assert (
        operator_copy.push_cloud_backup_failed(collection_id="docs", attempts=2).event
        == "collections.glacier_upload.failed"
    )


def test_notification_human_copy_avoids_machine_only_terms() -> None:
    forbidden = [term.casefold() for term in operator_copy.MACHINE_ONLY_TERMS]

    for notification, _metadata in _notification_contracts():
        texts = [
            notification.title,
            notification.body,
            notification.reminder_title or "",
            notification.reminder_body or "",
        ]
        rendered = "\n".join(texts).casefold()
        assert not [term for term in forbidden if term in rendered]


def test_copy_contract_defines_no_labeling_or_routine_success_notification() -> None:
    push_names = [
        name
        for name in dir(operator_copy)
        if name.startswith("push_") and callable(getattr(operator_copy, name))
    ]

    assert not [name for name in push_names if "label" in name]
    assert not [name for name in push_names if "success" in name or "done" in name]


def test_acceptance_copy_references_resolve_to_contract_functions() -> None:
    references = _acceptance_copy_references()

    assert references
    missing = [
        reference
        for reference in sorted(references)
        if not callable(getattr(operator_copy, reference, None))
    ]
    assert not missing


def test_feature_referenced_human_copy_avoids_machine_only_terms() -> None:
    forbidden = [term.casefold() for term in operator_copy.MACHINE_ONLY_TERMS]
    references = [
        reference
        for reference in sorted(_acceptance_copy_references())
        if not reference.startswith("push_")
    ]

    assert references
    for reference in references:
        rendered = _feature_copy_text(reference).casefold()
        assert not [term for term in forbidden if term in rendered]


def test_operator_guidance_lives_in_contract_copy() -> None:
    assert "replacement disc" in _feature_copy_text("disc_item_recovery_ready")
    assert "Run arc-disc" in _feature_copy_text("fetch_detail_pending")


def test_operator_formatting_is_plain_text_and_stable() -> None:
    assert operator_format.command("arc-disc") == "arc-disc"
    assert operator_format.raw_command("arc", "get", "docs/tax file.pdf") == (
        "arc get 'docs/tax file.pdf'"
    )
    assert operator_format.truncate("abcdefghij", max_chars=8) == "abcde..."
    assert operator_format.list_sentence(["docs", "photos", "video"]) == (
        "docs, photos, and video"
    )

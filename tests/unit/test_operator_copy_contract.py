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


def _notification_contracts() -> list[operator_copy.ActionNeededNotification]:
    return [
        operator_copy.push_burn_work_ready(
            disc_count=2,
            oldest_ready_at="2026-05-01 08:00 UTC",
        ),
        operator_copy.push_disc_work_waiting_too_long(
            disc_count=2,
            oldest_ready_at="2026-05-01 08:00 UTC",
        ),
        operator_copy.push_replacement_disc_needed(label_text="20260420T040001Z-3"),
        operator_copy.push_recovery_approval_required(
            affected=["docs"],
            estimated_cost="12.34",
        ),
        operator_copy.push_recovery_ready(
            affected=["docs"],
            expires_at="2026-05-02 08:00 UTC",
        ),
        operator_copy.push_hot_recovery_needs_media(
            target="docs/tax/2022/invoice-123.pdf",
        ),
        operator_copy.push_cloud_backup_failed(collection_id="docs", attempts=2),
        operator_copy.push_notification_health_failed(channel="Push"),
        operator_copy.push_billing_needs_attention(reason="pricing unavailable"),
        operator_copy.push_setup_needs_attention(area="Storage", summary="missing bucket"),
    ]


def test_notification_copy_payloads_match_action_needed_schema() -> None:
    validator = _schema_validator()

    for notification in _notification_contracts():
        payload = notification.payload(
            delivered_at="2026-05-01T08:00:00Z",
            reminder_count=0,
        )
        validator.validate(payload)

        action = payload["actions"][0]
        assert action["label"] == f"Run {action['command']}"
        assert action["argv"] == [action["command"]]
        assert "action" not in payload

        if notification.reminder_title or notification.reminder_body or notification.reminder_event:
            validator.validate(
                notification.payload(
                    reminder=True,
                    delivered_at="2026-05-01T09:00:00Z",
                    reminder_count=1,
                )
            )


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

    for notification in _notification_contracts():
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
    reference_pattern = re.compile(r'operator (?:notification )?copy "([^"]+)"')
    references: set[str] = set()
    for path in FEATURES_DIR.glob("*.feature"):
        references.update(reference_pattern.findall(path.read_text(encoding="utf-8")))

    assert references
    missing = [
        reference
        for reference in sorted(references)
        if not callable(getattr(operator_copy, reference, None))
    ]
    assert not missing


def test_operator_formatting_is_plain_text_and_stable() -> None:
    assert operator_format.command("arc-disc") == "arc-disc"
    assert operator_format.raw_command("arc", "get", "docs/tax file.pdf") == (
        "arc get 'docs/tax file.pdf'"
    )
    assert operator_format.truncate("abcdefghij", max_chars=8) == "abcde..."
    assert operator_format.list_sentence(["docs", "photos", "video"]) == (
        "docs, photos, and video"
    )

from __future__ import annotations

from munchy.notifications import MUNCHY_WEBHOOK_EMOJI, notification_payload


def test_munchy_notification_payload_uses_canonical_emoji() -> None:
    payload = notification_payload(
        event="job.received",
        message="Job received.",
        recipients=("operator",),
        extra={"job_id": "job-1"},
    )

    assert payload["source"] == "munchy"
    assert payload["emoji"] == MUNCHY_WEBHOOK_EMOJI == "🤤"
    assert payload["recipients"] == ["operator"]
    assert payload["job_id"] == "job-1"

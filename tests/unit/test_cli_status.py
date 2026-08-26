from __future__ import annotations

from riverhog_cli.main import _archive_wait_status
from riverhog_cli.output import format_collection_upload


def test_archive_wait_status_reports_the_current_phase() -> None:
    status = _archive_wait_status({"archive_phase": "planning"})

    assert status == ", archive_phase=planning"


def test_archive_wait_status_reports_the_exact_retry_schedule() -> None:
    status = _archive_wait_status(
        {
            "archive_phase": "retry_wait",
            "latest_failure": "temporary provider failure",
            "archive_next_attempt_at": "2026-08-26T12:34:56.000000Z",
        }
    )

    assert status == (
        ", archive_phase=retry_wait"
        ", latest_failure=temporary provider failure"
        ", archive_next_attempt_at=2026-08-26T12:34:56.000000Z"
    )

    rich = format_collection_upload(
        {
            "collection_id": 1,
            "state": "finalizing",
            "archive_phase": "retry_wait",
            "latest_failure": "temporary provider failure",
            "archive_next_attempt_at": "2026-08-26T12:34:56.000000Z",
        }
    )
    assert "archive phase: retry_wait" in rich
    assert "failure: temporary provider failure" in rich
    assert "archive next attempt: 2026-08-26T12:34:56.000000Z" in rich

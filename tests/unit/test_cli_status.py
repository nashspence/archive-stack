from __future__ import annotations

from riverhog_cli.main import _archive_wait_status


def test_archive_wait_status_reports_the_current_phase() -> None:
    status = _archive_wait_status({"archive_phase": "planning"})

    assert status == ", archive_phase=planning"

from __future__ import annotations

from riverhog_cli.main import _archive_wait_status


def test_archive_wait_status_names_object_planning_stage() -> None:
    status = _archive_wait_status({"archive_phase": "planning"})

    assert status == ", archive_phase=planning, planning archive objects"

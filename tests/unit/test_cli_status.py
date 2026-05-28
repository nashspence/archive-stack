from __future__ import annotations

from riverhog_cli.main import _archive_wait_status


def test_archive_wait_status_names_packaging_stage() -> None:
    status = _archive_wait_status({"archive_phase": "packaging"})

    assert status == ", archive_phase=packaging, building archive package"

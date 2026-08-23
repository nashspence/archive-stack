from __future__ import annotations

import os
import sys
from pathlib import Path

import gogurt.core as core
import pytest
from gogurt.mounts import discover_mount_points


def test_native_mount_discovery_observes_at_least_one_root() -> None:
    mount_points = discover_mount_points()

    assert mount_points
    assert any(path.is_dir() for path in mount_points)


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows file sharing")
def test_windows_marker_reader_allows_atomic_replacement(tmp_path: Path) -> None:
    marker = tmp_path / ".gogurt"
    marker.write_text("first-route\n", encoding="utf-8")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    descriptor = core._open_marker_descriptor(marker, flags)
    try:
        replacement = tmp_path / ".replacement"
        replacement.write_text("second-route\n", encoding="utf-8")
        os.replace(replacement, marker)
    finally:
        os.close(descriptor)

    assert marker.read_text(encoding="utf-8") == "second-route\n"

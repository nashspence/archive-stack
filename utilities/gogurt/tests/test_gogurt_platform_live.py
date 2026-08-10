from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from gogurt.mounts import discover_mount_points


def test_native_mount_discovery_observes_at_least_one_root() -> None:
    mount_points = discover_mount_points()

    assert mount_points
    assert any(path.is_dir() for path in mount_points)


def test_native_macos_event_adapter_typechecks_when_present() -> None:
    source = Path(__file__).resolve().parents[1] / "macos" / "gogurt.swift"
    assert source.is_file()
    if sys.platform != "darwin":
        return

    swiftc = shutil.which("swiftc")
    assert swiftc is not None
    completed = subprocess.run(
        [swiftc, "-typecheck", "-framework", "AppKit", str(source)],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr

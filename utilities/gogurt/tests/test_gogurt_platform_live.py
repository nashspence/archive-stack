from __future__ import annotations

import sys


def _discover_mount_points():
    if sys.platform == "linux":
        from gogurt_linux import MOUNT_PROVIDER_BINDING
    elif sys.platform == "darwin":
        from gogurt_macos import MOUNT_PROVIDER_BINDING
    elif sys.platform == "win32":
        from gogurt_windows import MOUNT_PROVIDER_BINDING
    else:  # pragma: no cover - the release matrix owns the reference fixtures
        raise AssertionError(f"no reference mount provider for {sys.platform}")
    return MOUNT_PROVIDER_BINDING.discover()


def test_native_mount_discovery_observes_at_least_one_root() -> None:
    mount_points = _discover_mount_points()

    assert mount_points
    assert any(path.is_dir() for path in mount_points)

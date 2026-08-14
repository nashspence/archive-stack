from __future__ import annotations

from gogurt.mounts import discover_mount_points


def test_native_mount_discovery_observes_at_least_one_root() -> None:
    mount_points = discover_mount_points()

    assert mount_points
    assert any(path.is_dir() for path in mount_points)

"""macOS mounted-volume access for Gogurt."""

from __future__ import annotations

from pathlib import Path

from gogurt_core.mounts import MountedVolumeProviderBinding
from gogurt_path_volume_support import PathMountedVolumeAccess


def macos_mount_points(volumes_dir: Path = Path("/Volumes")) -> tuple[Path, ...]:
    points = {Path("/")}
    if volumes_dir.is_dir():
        points.update(volumes_dir.iterdir())
    return tuple(sorted(points, key=lambda path: str(path).casefold()))


def discover_mount_points() -> tuple[Path, ...]:
    return macos_mount_points()


MOUNTED_VOLUME_PROVIDER_BINDING = MountedVolumeProviderBinding(
    provider_id="gogurt-macos-path-route-line-provider/v1",
    access=PathMountedVolumeAccess(discover_mount_points),
)


__all__ = [
    "MOUNTED_VOLUME_PROVIDER_BINDING",
    "discover_mount_points",
    "macos_mount_points",
]

"""Linux mounted-volume access for Gogurt."""

from __future__ import annotations

import re
from pathlib import Path

from gogurt_core.mounts import MountedVolumeProviderBinding
from gogurt_path_volume_support import PathMountedVolumeAccess

_MOUNTINFO_ESCAPE_RE = re.compile(r"\\([0-7]{3})")


def linux_mount_points(mountinfo: str) -> tuple[Path, ...]:
    points = {
        Path(_MOUNTINFO_ESCAPE_RE.sub(lambda match: chr(int(match.group(1), 8)), fields[4]))
        for line in mountinfo.splitlines()
        if len(fields := line.split()) >= 5
    }
    return tuple(sorted(points, key=lambda path: str(path)))


def discover_mount_points() -> tuple[Path, ...]:
    return linux_mount_points(Path("/proc/self/mountinfo").read_text(encoding="utf-8"))


MOUNTED_VOLUME_PROVIDER_BINDING = MountedVolumeProviderBinding(
    provider_id="gogurt-linux-path-route-line-provider/v1",
    access=PathMountedVolumeAccess(discover_mount_points),
)


__all__ = [
    "MOUNTED_VOLUME_PROVIDER_BINDING",
    "discover_mount_points",
    "linux_mount_points",
]

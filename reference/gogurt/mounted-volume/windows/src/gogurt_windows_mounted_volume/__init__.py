"""Windows mounted-volume access for Gogurt."""

from __future__ import annotations

import ctypes
import string
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from gogurt_core.mounts import MountedVolumeProviderBinding
from gogurt_path_volume_support import PathMountedVolumeAccess


def _windows_logical_drive_mask() -> int:
    mask = int(cast(Any, ctypes).windll.kernel32.GetLogicalDrives())
    if mask == 0:
        raise OSError("Windows logical-drive enumeration failed")
    return mask


def windows_mount_points(
    logical_drives: Callable[[], int] = _windows_logical_drive_mask,
) -> tuple[Path, ...]:
    mask = logical_drives()
    return tuple(
        Path(f"{letter}:\\")
        for index, letter in enumerate(string.ascii_uppercase)
        if mask & (1 << index)
    )


def discover_mount_points() -> tuple[Path, ...]:
    return windows_mount_points()


MOUNTED_VOLUME_PROVIDER_BINDING = MountedVolumeProviderBinding(
    provider_id="gogurt-windows-path-route-line-provider/v1",
    access=PathMountedVolumeAccess(discover_mount_points),
)


__all__ = [
    "MOUNTED_VOLUME_PROVIDER_BINDING",
    "discover_mount_points",
    "windows_mount_points",
]

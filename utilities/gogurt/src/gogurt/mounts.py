from __future__ import annotations

import math
import re
import string
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

_MOUNTINFO_ESCAPE_RE = re.compile(r"\\([0-7]{3})")
MIN_GOGURT_INTERVAL_SECONDS = 0.1
MAX_GOGURT_INTERVAL_SECONDS = 3600.0


def validate_gogurt_interval(value: object) -> float:
    """Return a finite polling interval within the supported v1 range."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Gogurt polling interval must be a number")
    interval = float(value)
    if not math.isfinite(interval):
        raise ValueError("Gogurt polling interval must be finite")
    if not MIN_GOGURT_INTERVAL_SECONDS <= interval <= MAX_GOGURT_INTERVAL_SECONDS:
        raise ValueError(
            "Gogurt polling interval must be between "
            f"{MIN_GOGURT_INTERVAL_SECONDS} and {MAX_GOGURT_INTERVAL_SECONDS} seconds"
        )
    return interval


def _decode_mountinfo_path(value: str) -> str:
    return _MOUNTINFO_ESCAPE_RE.sub(lambda match: chr(int(match.group(1), 8)), value)


def linux_mount_points(mountinfo: str) -> tuple[Path, ...]:
    points = {
        Path(_decode_mountinfo_path(fields[4]))
        for line in mountinfo.splitlines()
        if len(fields := line.split()) >= 5
    }
    # mountinfo is the presence authority. Readability belongs to the later
    # per-mount planning result and must not manufacture an unmount/remount.
    return tuple(sorted(points, key=lambda path: str(path)))


def macos_mount_points(volumes_dir: Path = Path("/Volumes")) -> tuple[Path, ...]:
    points = {Path("/")}
    if volumes_dir.is_dir():
        # Directory entries are the mount-presence observation. Do not use
        # path.is_dir(), which conflates a mounted but temporarily unreadable
        # volume with removal.
        points.update(volumes_dir.iterdir())
    return tuple(sorted(points, key=lambda path: str(path).casefold()))


def _windows_logical_drive_mask() -> int:
    import ctypes

    mask = int(ctypes.windll.kernel32.GetLogicalDrives())  # type: ignore[attr-defined]
    if mask == 0:
        raise OSError("Windows logical-drive enumeration failed")
    return mask


def windows_mount_points(
    logical_drives: Callable[[], int] = _windows_logical_drive_mask,
) -> tuple[Path, ...]:
    mask = logical_drives()
    points = [
        Path(f"{letter}:\\")
        for index, letter in enumerate(string.ascii_uppercase)
        if mask & (1 << index)
    ]
    return tuple(points)


def discover_mount_points(platform: str | None = None) -> tuple[Path, ...]:
    current = platform or sys.platform
    if current.startswith("linux"):
        return linux_mount_points(Path("/proc/self/mountinfo").read_text(encoding="utf-8"))
    if current == "darwin":
        return macos_mount_points()
    if current == "win32":
        return windows_mount_points()
    raise RuntimeError(f"Gogurt does not support mount discovery on {current}")


def iter_new_mounts(
    *,
    discover: Callable[[], Sequence[Path]] = discover_mount_points,
    interval_seconds: float = 2.0,
    include_existing: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[Path]:
    interval = validate_gogurt_interval(interval_seconds)
    known = set() if include_existing else set(discover())
    while True:
        current = set(discover())
        yield from sorted(current - known, key=lambda path: str(path).casefold())
        known = current
        sleep(interval)

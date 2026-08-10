from __future__ import annotations

import os
import re
import string
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

_MOUNTINFO_ESCAPE_RE = re.compile(r"\\([0-7]{3})")


def _decode_mountinfo_path(value: str) -> str:
    return _MOUNTINFO_ESCAPE_RE.sub(lambda match: chr(int(match.group(1), 8)), value)


def _is_accessible_directory(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def linux_mount_points(mountinfo: str) -> tuple[Path, ...]:
    points = {
        Path(_decode_mountinfo_path(fields[4]))
        for line in mountinfo.splitlines()
        if len(fields := line.split()) >= 5
    }
    return tuple(
        sorted(
            (path for path in points if _is_accessible_directory(path)),
            key=lambda path: str(path),
        )
    )


def macos_mount_points(volumes_dir: Path = Path("/Volumes")) -> tuple[Path, ...]:
    points = {Path("/")}
    if volumes_dir.is_dir():
        points.update(path for path in volumes_dir.iterdir() if path.is_dir())
    return tuple(sorted(points, key=lambda path: str(path).casefold()))


def windows_mount_points(
    is_dir: Callable[[str], bool] = os.path.isdir,
) -> tuple[Path, ...]:
    points = [Path(f"{letter}:\\") for letter in string.ascii_uppercase if is_dir(f"{letter}:\\")]
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
    if interval_seconds <= 0:
        raise ValueError("Gogurt polling interval must be positive")
    known = set() if include_existing else set(discover())
    while True:
        current = set(discover())
        yield from sorted(current - known, key=lambda path: str(path).casefold())
        known = current
        sleep(interval_seconds)

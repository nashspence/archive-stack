"""Conventional local-path mechanics for optional Gogurt providers."""

from __future__ import annotations

import os
import stat
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from config_validation import ConfigError
from gogurt_core.mounts import MountedMarkerSnapshot

PORTABLE_MARKER_MODE = 0o644
WINDOWS_PROMOTION_SETTLE_SECONDS = 1.0
WINDOWS_PROMOTION_RETRY_SECONDS = 0.01
WINDOWS_TRANSIENT_PROMOTION_ERRORS = frozenset({5, 32})


def _marker_identity(info: os.stat_result, content: bytes) -> str:
    payload = "\0".join(
        (
            str(info.st_dev),
            str(info.st_ino),
            str(info.st_size),
            str(info.st_mtime_ns),
            sha256(content).hexdigest(),
        )
    )
    return sha256(payload.encode("ascii")).hexdigest()


def _require_volume_root(mount_point: Path) -> None:
    info = mount_point.lstat()
    if not stat.S_ISDIR(info.st_mode) or mount_point.is_symlink():
        raise NotADirectoryError(mount_point)


def _set_portable_marker_mode(path: Path) -> None:
    if os.name != "nt":
        # Markers contain only non-secret routing metadata and must remain
        # readable when portable media changes ordinary local-user custody.
        os.chmod(path, PORTABLE_MARKER_MODE, follow_symlinks=False)


def _publish_complete_file(destination: Path, content: bytes) -> None:
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=".gogurt-",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(raw_temporary)
    try:
        stream = os.fdopen(descriptor, "wb")
        descriptor = -1
        with stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _set_portable_marker_mode(temporary)
        if os.name != "nt":
            os.replace(temporary, destination)
            return
        deadline = time.monotonic() + WINDOWS_PROMOTION_SETTLE_SECONDS
        while True:
            try:
                os.replace(temporary, destination)
                return
            except PermissionError as exc:
                if (
                    getattr(exc, "winerror", None) not in WINDOWS_TRANSIENT_PROMOTION_ERRORS
                    or time.monotonic() >= deadline
                ):
                    raise
                time.sleep(WINDOWS_PROMOTION_RETRY_SECONDS)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class PathMountedVolumeAccess:
    """Safe marker mechanics for ordinary local path-mounted volumes."""

    discover_mounts: Callable[[], Sequence[Path]]

    def discover(self) -> Sequence[Path]:
        return self.discover_mounts()

    def observe_marker(
        self,
        mount_point: Path,
        marker_name: str,
        *,
        max_bytes: int,
    ) -> MountedMarkerSnapshot | None:
        _require_volume_root(mount_point)
        marker = mount_point / marker_name
        try:
            initial = marker.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(initial.st_mode) or marker.is_symlink():
            raise ConfigError(f"gogurt marker must be a regular file: {marker}")
        if initial.st_size > max_bytes:
            raise ConfigError(f"gogurt marker exceeds {max_bytes} bytes: {marker}")

        flags = os.O_RDONLY
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(marker, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise ConfigError(f"gogurt marker must be a regular file: {marker}")
            if opened.st_size > max_bytes:
                raise ConfigError(f"gogurt marker exceeds {max_bytes} bytes: {marker}")
            content = os.read(descriptor, max_bytes + 1)
            final = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if len(content) > max_bytes:
            raise ConfigError(f"gogurt marker exceeds {max_bytes} bytes: {marker}")
        if (
            initial.st_dev,
            initial.st_ino,
            initial.st_size,
            initial.st_mtime_ns,
        ) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ) or (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ) != (
            final.st_dev,
            final.st_ino,
            final.st_size,
            final.st_mtime_ns,
        ):
            raise ConfigError(f"gogurt marker changed while it was read: {marker}")
        return MountedMarkerSnapshot(content=content, identity=_marker_identity(final, content))

    def publish_marker(
        self,
        mount_point: Path,
        marker_name: str,
        content: bytes,
    ) -> MountedMarkerSnapshot:
        _require_volume_root(mount_point)
        marker = mount_point / marker_name
        _publish_complete_file(marker, content)
        observed = self.observe_marker(
            mount_point,
            marker_name,
            max_bytes=len(content),
        )
        if observed is None:
            raise OSError(f"Gogurt marker was absent after publication: {marker}")
        return observed


__all__ = ["PathMountedVolumeAccess"]

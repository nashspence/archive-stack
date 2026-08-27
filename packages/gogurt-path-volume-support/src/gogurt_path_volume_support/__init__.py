"""Conventional local-path mechanics for optional Gogurt providers."""

from __future__ import annotations

import os
import stat
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from config_validation import ConfigError
from gogurt_core.mounts import GogurtRouteMarker, MountedMarkerObservation

PATH_MARKER_NAME = ".gogurt"
_PATH_MARKER_PUBLICATION_LOCK_NAME = ".gogurt.publish.lock"
MAX_PATH_MARKER_BYTES = 4096
PORTABLE_MARKER_MODE = 0o644
_PUBLICATION_LOCK_MODE = 0o666
WINDOWS_PROMOTION_SETTLE_SECONDS = 1.0
WINDOWS_PROMOTION_RETRY_SECONDS = 0.01
WINDOWS_TRANSIENT_PROMOTION_ERRORS = frozenset({5, 32})


@contextmanager
def _marker_publication_lock(mount_point: Path) -> Iterator[None]:
    """Serialize conditional publications made through this path provider."""

    lock_path = mount_point / _PATH_MARKER_PUBLICATION_LOCK_NAME
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, _PUBLICATION_LOCK_MODE)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError(f"gogurt marker publication lock is not regular: {lock_path}")
        if os.name != "nt":
            os.fchmod(descriptor, _PUBLICATION_LOCK_MODE)
        if os.name == "nt":
            import msvcrt

            windows_locking = cast(Any, msvcrt)
            if info.st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            windows_locking.locking(descriptor, windows_locking.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                windows_locking = cast(Any, msvcrt)
                os.lseek(descriptor, 0, os.SEEK_SET)
                windows_locking.locking(descriptor, windows_locking.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


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
    """Safe `.gogurt` line representation for ordinary local path volumes."""

    discover_mounts: Callable[[], Sequence[Path]]

    def discover(self) -> Sequence[Path]:
        return self.discover_mounts()

    def observe_marker(
        self,
        mount_point: Path,
    ) -> MountedMarkerObservation | None:
        _require_volume_root(mount_point)
        marker = mount_point / PATH_MARKER_NAME
        try:
            initial = marker.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(initial.st_mode) or marker.is_symlink():
            raise ConfigError(f"gogurt marker must be a regular file: {marker}")
        if initial.st_size > MAX_PATH_MARKER_BYTES:
            raise ConfigError(f"gogurt path marker exceeds {MAX_PATH_MARKER_BYTES} bytes: {marker}")

        flags = os.O_RDONLY
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(marker, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise ConfigError(f"gogurt marker must be a regular file: {marker}")
            if opened.st_size > MAX_PATH_MARKER_BYTES:
                raise ConfigError(
                    f"gogurt path marker exceeds {MAX_PATH_MARKER_BYTES} bytes: {marker}"
                )
            content = os.read(descriptor, MAX_PATH_MARKER_BYTES + 1)
            final = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if len(content) > MAX_PATH_MARKER_BYTES:
            raise ConfigError(f"gogurt path marker exceeds {MAX_PATH_MARKER_BYTES} bytes: {marker}")
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
        try:
            text = content.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ConfigError(f"gogurt path marker must be strict UTF-8: {marker}") from exc
        lines = text.splitlines()
        if len(lines) != 1 or not lines[0] or lines[0] != lines[0].strip():
            raise ConfigError(f"gogurt path marker must contain one exact route: {marker}")
        try:
            document = GogurtRouteMarker(lines[0])
        except ValueError as exc:
            raise ConfigError(f"invalid gogurt route in path marker: {marker}") from exc
        return MountedMarkerObservation(
            marker=document,
            identity=_marker_identity(final, content),
        )

    def publish_marker(
        self,
        mount_point: Path,
        document: GogurtRouteMarker,
        *,
        expected: MountedMarkerObservation | None,
    ) -> MountedMarkerObservation:
        _require_volume_root(mount_point)
        if not isinstance(document, GogurtRouteMarker):
            raise TypeError("Gogurt path-volume publication requires a logical route marker")
        marker = mount_point / PATH_MARKER_NAME
        content = f"{document.route}\n".encode()
        with _marker_publication_lock(mount_point):
            current = self.observe_marker(mount_point)
            if expected is None:
                if current is not None:
                    raise FileExistsError(f"gogurt marker appeared before publication: {marker}")
            elif current != expected:
                raise ConfigError(f"gogurt marker changed before publication: {marker}")
            _publish_complete_file(marker, content)
            observed = self.observe_marker(mount_point)
        if observed is None:
            raise OSError(f"Gogurt marker was absent after publication: {marker}")
        if observed.marker != document:
            raise ConfigError(f"Gogurt path marker differs after publication: {marker}")
        return observed


__all__ = [
    "MAX_PATH_MARKER_BYTES",
    "PATH_MARKER_NAME",
    "PORTABLE_MARKER_MODE",
    "PathMountedVolumeAccess",
]

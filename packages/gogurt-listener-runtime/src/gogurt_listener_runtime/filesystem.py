from __future__ import annotations

import os
import stat
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path
from typing import TextIO

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
WINDOWS_PROMOTION_SETTLE_SECONDS = 1.0
WINDOWS_PROMOTION_RETRY_SECONDS = 0.01
WINDOWS_TRANSIENT_PROMOTION_ERRORS = frozenset({5, 32})


def _set_private_directory_mode(path: Path) -> None:
    if os.name != "nt":
        os.chmod(path, PRIVATE_DIRECTORY_MODE, follow_symlinks=False)


def _set_private_file_mode(path: Path) -> None:
    if os.name != "nt":
        os.chmod(path, PRIVATE_FILE_MODE, follow_symlinks=False)


def _set_staged_file_mode(path: Path, mode: int) -> None:
    if mode != PRIVATE_FILE_MODE:
        raise ValueError(f"unsupported Gogurt listener file mode: {mode:o}")
    _set_private_file_mode(path)


def ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise OSError(f"Gogurt listener state path is not a directory: {path}")
    _set_private_directory_mode(path)


def ensure_private_file(path: Path) -> None:
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise OSError(f"Gogurt listener state path is not a regular file: {path}")
        if os.name == "nt":
            # Windows state inherits the current-user directory ACL. Reopening
            # an active SQLite sidecar solely to emulate chmod can conflict
            # with SQLite's mandatory sharing mode.
            return
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, PRIVATE_FILE_MODE)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, PRIVATE_FILE_MODE)
    finally:
        os.close(descriptor)


def ensure_private_files(paths: Iterable[Path]) -> None:
    """Normalize files which still exist without ever recreating transient paths."""

    for path in paths:
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise OSError(f"Gogurt listener state path is not a regular file: {path}")
        if os.name == "nt":
            continue
        flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            # SQLite may remove a transient WAL sidecar between directory
            # enumeration and validation. It must never be recreated here.
            continue
        try:
            os.fchmod(descriptor, PRIVATE_FILE_MODE)
        finally:
            os.close(descriptor)


def stage_bytes(destination: Path, content: bytes, *, mode: int) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(
        # Keep staging names implementation-owned and bounded rather than
        # extending a destination near the portable component ceiling.
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
        _set_staged_file_mode(temporary, mode)
        return temporary
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


def promote_staged(temporary: Path, destination: Path, *, mode: int) -> None:
    _set_staged_file_mode(temporary, mode)
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


def atomic_write(destination: Path, content: bytes, *, mode: int) -> None:
    temporary = stage_bytes(destination, content, mode=mode)
    try:
        promote_staged(temporary, destination, mode=mode)
    finally:
        temporary.unlink(missing_ok=True)


def open_private_text_append(
    path: Path,
    *,
    encoding: str,
    errors: str | None,
) -> TextIO:
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, PRIVATE_FILE_MODE)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, PRIVATE_FILE_MODE)
        stream = os.fdopen(descriptor, "a", encoding=encoding, errors=errors)
        descriptor = -1
        return stream
    finally:
        if descriptor >= 0:
            os.close(descriptor)

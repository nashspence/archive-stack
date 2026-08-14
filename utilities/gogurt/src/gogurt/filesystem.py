from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import TextIO

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
PORTABLE_FILE_MODE = 0o644


def _set_private_directory_mode(path: Path) -> None:
    if os.name != "nt":
        os.chmod(path, PRIVATE_DIRECTORY_MODE, follow_symlinks=False)


def _set_private_file_mode(path: Path) -> None:
    if os.name != "nt":
        os.chmod(path, PRIVATE_FILE_MODE, follow_symlinks=False)


def _set_portable_file_mode(path: Path) -> None:
    if os.name != "nt":
        # Volume markers contain only non-secret routing metadata and must remain
        # readable when portable media moves between ordinary local users.
        os.chmod(path, PORTABLE_FILE_MODE, follow_symlinks=False)


def _set_staged_file_mode(path: Path, mode: int) -> None:
    if mode == PRIVATE_FILE_MODE:
        _set_private_file_mode(path)
        return
    if mode == PORTABLE_FILE_MODE:
        _set_portable_file_mode(path)
        return
    raise ValueError(f"unsupported Gogurt file mode: {mode:o}")


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
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, PRIVATE_FILE_MODE)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, PRIVATE_FILE_MODE)
    finally:
        os.close(descriptor)


def ensure_private_files(paths: Iterable[Path]) -> None:
    for path in paths:
        if path.exists() or path.is_symlink():
            ensure_private_file(path)


def stage_bytes(destination: Path, content: bytes, *, mode: int) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.",
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
    os.replace(temporary, destination)


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

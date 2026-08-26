from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

PORTABLE_FILE_MODE = 0o644
WINDOWS_PROMOTION_SETTLE_SECONDS = 1.0
WINDOWS_PROMOTION_RETRY_SECONDS = 0.01
WINDOWS_TRANSIENT_PROMOTION_ERRORS = frozenset({5, 32})


def _set_portable_file_mode(path: Path) -> None:
    if os.name != "nt":
        # Volume markers contain only non-secret routing metadata and must remain
        # readable when portable media moves between ordinary local users.
        os.chmod(path, PORTABLE_FILE_MODE, follow_symlinks=False)


def atomic_write(destination: Path, content: bytes, *, mode: int) -> None:
    if mode != PORTABLE_FILE_MODE:
        raise ValueError(f"unsupported Gogurt core file mode: {mode:o}")
    destination.parent.mkdir(parents=True, exist_ok=True)
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
        _set_portable_file_mode(temporary)
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

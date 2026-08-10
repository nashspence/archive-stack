from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

from .errors import ProvenanceObserverError

INSTALLATION_ID_FILENAME = "provenance-installation-id"


def user_installation_id(application: str) -> str:
    """Return an opaque identity persisted in the application's user state."""

    name = application.strip()
    if not name or name != application or any(char in name for char in "/\\"):
        raise ValueError("application must be a canonical path-segment name")
    return load_or_create_installation_id(_user_state_root() / name / INSTALLATION_ID_FILENAME)


def load_or_create_installation_id(path: Path) -> str:
    """Load or atomically establish an opaque installation-scoped UUID URN."""

    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(resolved, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _read_installation_id(resolved)

    identity = f"urn:uuid:{uuid.uuid4()}"
    try:
        with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as stream:
            stream.write(identity + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            resolved.unlink()
        except OSError:
            pass
        raise
    return identity


def _read_installation_id(path: Path) -> str:
    # Another process can win creation immediately before it writes the short value.
    value = ""
    for _attempt in range(20):
        value = path.read_text(encoding="ascii").strip()
        if value:
            break
        time.sleep(0.01)
    try:
        parsed = uuid.UUID(value.removeprefix("urn:uuid:"))
    except (ValueError, AttributeError) as exc:
        raise ProvenanceObserverError(f"installation identity is not a UUID URN: {path}") from exc
    canonical = f"urn:uuid:{parsed}"
    if value != canonical:
        raise ProvenanceObserverError(f"installation identity is not canonical: {path}")
    return canonical


def _user_state_root() -> Path:
    configured_root = os.getenv("RIVERHOG_PROVENANCE_STATE_HOME")
    if configured_root:
        return Path(configured_root).expanduser()
    if sys.platform == "win32":
        configured = os.getenv("LOCALAPPDATA")
        return Path(configured) if configured else Path.home() / "AppData" / "Local"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    configured = os.getenv("XDG_STATE_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".local" / "state"


__all__ = [
    "INSTALLATION_ID_FILENAME",
    "load_or_create_installation_id",
    "user_installation_id",
]

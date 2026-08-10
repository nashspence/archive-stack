from __future__ import annotations

import sys

from .errors import UnsupportedPlatformError
from .interface import FileStateObserver
from .linux import UbuntuFileStateObserver
from .macos import MacOSFileStateObserver
from .windows import WindowsFileStateObserver


def get_observer(platform_name: str = "auto") -> FileStateObserver:
    """Return the reference observer for Windows, macOS, or Ubuntu/Linux."""

    normalized = platform_name.strip().lower()
    if normalized in {"windows", "win", "win32"}:
        return WindowsFileStateObserver()
    if normalized in {"macos", "mac", "osx", "darwin"}:
        return MacOSFileStateObserver()
    if normalized in {"ubuntu", "linux"}:
        return UbuntuFileStateObserver(enforce_ubuntu=(normalized == "ubuntu"))
    if normalized != "auto":
        raise ValueError(f"unknown observer platform: {platform_name}")
    if sys.platform == "win32":
        return WindowsFileStateObserver()
    if sys.platform == "darwin":
        return MacOSFileStateObserver()
    if sys.platform.startswith("linux"):
        return UbuntuFileStateObserver()
    raise UnsupportedPlatformError(f"no Riverhog provenance observer for {sys.platform!r}")

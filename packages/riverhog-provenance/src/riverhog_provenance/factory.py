from __future__ import annotations

import sys

from .errors import UnsupportedPlatformError
from .interface import FileStateObserver
from .linux import LinuxFileStateObserver
from .macos import MacOSFileStateObserver
from .windows import WindowsFileStateObserver


def get_observer(platform_name: str = "auto") -> FileStateObserver:
    """Return the reference observer for Windows, macOS, or Linux."""

    normalized = platform_name.strip().lower()
    if normalized == "windows":
        return WindowsFileStateObserver()
    if normalized == "macos":
        return MacOSFileStateObserver()
    if normalized == "linux":
        return LinuxFileStateObserver()
    if normalized != "auto":
        raise ValueError(f"unknown observer platform: {platform_name}")
    if sys.platform == "win32":
        return WindowsFileStateObserver()
    if sys.platform == "darwin":
        return MacOSFileStateObserver()
    if sys.platform.startswith("linux"):
        return LinuxFileStateObserver()
    raise UnsupportedPlatformError(f"no Riverhog provenance observer for {sys.platform!r}")

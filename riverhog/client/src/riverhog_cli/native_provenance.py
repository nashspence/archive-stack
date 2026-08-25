"""Select the one native provenance observer in this client installation."""

from __future__ import annotations

import sys

from riverhog_provenance import FileStateObserver, UnsupportedPlatformError


def native_provenance_observer() -> FileStateObserver:
    """Return the observer selected by the release artifact's platform closure."""

    if sys.platform.startswith("linux"):
        from riverhog_provenance_linux_observer import LinuxFileStateObserver

        return LinuxFileStateObserver()
    if sys.platform == "darwin":
        from riverhog_provenance_macos_observer import MacOSFileStateObserver

        return MacOSFileStateObserver()
    if sys.platform == "win32":
        from riverhog_provenance_windows_observer import WindowsFileStateObserver

        return WindowsFileStateObserver()
    raise UnsupportedPlatformError(f"no Riverhog provenance observer for {sys.platform!r}")


__all__ = ["native_provenance_observer"]

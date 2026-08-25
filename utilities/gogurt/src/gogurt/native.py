"""Select the one native Gogurt implementation installed for this host."""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from gogurt_core.platform import ListenerAdapter, ListenerPaths

if sys.platform.startswith("linux"):
    from gogurt_linux import (
        default_listener_paths,
        discover_mount_points,
        listener_adapter,
        resolve_listener_executable,
    )
elif sys.platform == "darwin":
    from gogurt_macos import (
        default_listener_paths,
        discover_mount_points,
        listener_adapter,
        resolve_listener_executable,
    )
elif sys.platform == "win32":
    from gogurt_windows import (
        default_listener_paths,
        discover_mount_points,
        listener_adapter,
        resolve_listener_executable,
    )
else:
    raise RuntimeError(f"Gogurt does not support native integration on {sys.platform}")

_default_listener_paths: Callable[[], ListenerPaths] = default_listener_paths
_discover_mount_points: Callable[[], Sequence[Path]] = discover_mount_points
_listener_adapter: Callable[[], ListenerAdapter] = listener_adapter
_resolve_listener_executable: Callable[[str | None], Path] = resolve_listener_executable

__all__ = [
    "default_listener_paths",
    "discover_mount_points",
    "listener_adapter",
    "resolve_listener_executable",
]

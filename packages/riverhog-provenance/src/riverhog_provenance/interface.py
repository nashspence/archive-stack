from __future__ import annotations

import os
import stat as statmod
from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable

from .errors import SymlinkRefusedError, UnsupportedFileTypeError
from .model import (
    JsonObject,
    NativeCollection,
    NativeStat,
    ObservationRequest,
    ObservationResult,
    PathInput,
)


@runtime_checkable
class FileStateObserver(Protocol):
    """Clean platform-neutral interface for a Riverhog provenance state observer."""

    platform_family: str

    def observe(self, request: ObservationRequest) -> ObservationResult:
        """Observe one regular-file state and return schema-shaped assertions."""
        ...


class PlatformBackend(ABC):
    """Native-services contract used by the shared descriptor capture engine.

    The public observer protocol is intentionally tiny.  These backend hooks
    isolate pathname syntax, native identity, and platform timestamp behavior so
    the capture engine does not accidentally impose POSIX semantics on Windows.
    """

    platform_family: str

    @abstractmethod
    def assert_supported(self) -> None:
        """Raise when the backend cannot operate on the current host."""

    def absolute_path(self, path: PathInput) -> str | bytes:
        raw = os.fspath(path)
        if not isinstance(raw, (str, bytes)):
            raise TypeError("path must resolve to str or bytes")
        return os.path.abspath(raw)

    def preflight_path(self, path: str | bytes) -> None:
        result = os.lstat(path)
        if statmod.S_ISLNK(result.st_mode):
            raise SymlinkRefusedError("refusing to observe a symbolic-link final component")
        if not statmod.S_ISREG(result.st_mode):
            raise UnsupportedFileTypeError("target is not a regular file")

    def path_matches(self, path: str | bytes, stat: NativeStat) -> bool:
        try:
            result = os.lstat(path)
        except (FileNotFoundError, OSError):
            return False
        return statmod.S_ISREG(result.st_mode) and (result.st_dev, result.st_ino) == (
            stat.device,
            stat.inode,
        )

    def locator(
        self,
        path: str | bytes,
        *,
        kind: str,
        authority_id: str | None = None,
    ) -> JsonObject:
        # Import lazily to avoid a module cycle while common.py imports this ABC.
        from .common import locator_from_path

        return locator_from_path(path, kind=kind, authority_id=authority_id)

    def path_basename(self, path: str | bytes) -> str | bytes:
        return os.path.basename(path)

    def path_is_absolute(self, path: str | bytes) -> bool:
        return os.path.isabs(path)

    @abstractmethod
    def open_readonly(
        self, path: str | bytes, request: ObservationRequest
    ) -> tuple[int, list[dict[str, object]], bool]:
        """Open without following the final symlink.

        Returns ``(fd, diagnostics, noatime_effective)``.
        """

    @abstractmethod
    def stat_fd(self, fd: int) -> NativeStat:
        """Read a source-native descriptor stat snapshot."""

    @abstractmethod
    def collect(
        self,
        fd: int,
        path: str | bytes,
        stat: NativeStat,
        request: ObservationRequest,
    ) -> NativeCollection:
        """Capture platform-native metadata and the technical environment."""

    def finalize_timestamps(
        self,
        collection: NativeCollection,
        final_stat: NativeStat,
        request: ObservationRequest,
    ) -> None:
        """Update observer-affected timestamps after all reads.

        POSIX backends store nanoseconds in NativeStat.  Windows overrides this
        hook to retain the source FILETIME tick value rather than reverse-
        converting through nanoseconds.
        """
        if not request.policy.include_access_time:
            return
        from .common import format_utc_ns

        for timestamp in collection.timestamps:
            if timestamp.get("kind") == "accessed":
                timestamp["value"] = format_utc_ns(final_stat.atime_ns)
                timestamp["raw_value"] = str(final_stat.atime_ns)

    def release_fd(self, fd: int) -> None:
        """Release backend bookkeeping immediately before the engine closes fd."""
        return None

    def stability_differences(self, before: NativeStat, after: NativeStat) -> list[str]:
        labels = (
            "device",
            "inode",
            "mode",
            "nlink",
            "uid",
            "gid",
            "size",
            "mtime_ns",
            "ctime_ns",
            "flags",
            "generation",
        )
        return [
            label
            for label, left, right in zip(
                labels, before.stability_key(), after.stability_key(), strict=True
            )
            if left != right
        ]

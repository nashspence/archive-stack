"""Authoritative Jeb source landing-root resolution."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable
from pathlib import Path

from jeb_protocol import SourceIdError, source_id

from jeb_core.domain.sources import SourceRegistryError


class SourceRootResolver:
    """Map canonical source identities to unambiguous managed landing children."""

    def __init__(self, landing_dir: Path, *, managed_paths: Iterable[Path]) -> None:
        self.landing_dir = landing_dir
        self.managed_paths = tuple(managed_paths)

    def initialize(self) -> None:
        self.landing_dir.mkdir(parents=True, exist_ok=True)
        self._require_real_directory(self.landing_dir, label="landing root")
        self._reserved_source_ids()

    def root(self, value: str, *, create: bool = False) -> Path:
        try:
            normalized = source_id(value)
        except SourceIdError as exc:
            raise SourceRegistryError(str(exc)) from exc
        if normalized in self._reserved_source_ids():
            raise SourceRegistryError(f"source conflicts with a Jeb-managed landing path: {value}")

        landing = self.landing_dir.resolve()
        candidate = self.landing_dir / normalized
        if create:
            self.initialize()
            try:
                candidate.mkdir(mode=0o770)
            except FileExistsError:
                pass
        if candidate.exists() or candidate.is_symlink():
            self._require_real_directory(candidate, label=f"source landing root {normalized}")
            if candidate.resolve().parent != landing:
                raise SourceRegistryError(
                    f"source landing root escaped its configured parent: {normalized}"
                )
        elif candidate.parent.resolve() != landing:
            raise SourceRegistryError(
                f"source landing root escaped its configured parent: {normalized}"
            )
        return candidate

    def _reserved_source_ids(self) -> frozenset[str]:
        landing = self.landing_dir.resolve()
        landing_lexical = self.landing_dir.absolute()
        reserved: set[str] = set()
        for configured in self.managed_paths:
            lexical = configured.absolute()
            if lexical == landing_lexical:
                raise SourceRegistryError("a Jeb-managed path cannot be the landing root")
            if lexical.is_relative_to(landing_lexical):
                lexical_relative = lexical.relative_to(landing_lexical)
                if lexical_relative.parts:
                    try:
                        reserved.add(source_id(lexical_relative.parts[0]))
                    except SourceIdError:
                        pass
            resolved = configured.resolve(strict=False)
            if resolved == landing:
                raise SourceRegistryError("a Jeb-managed path cannot be the landing root")
            if not resolved.is_relative_to(landing):
                continue
            relative = resolved.relative_to(landing)
            if not relative.parts:
                raise SourceRegistryError("a Jeb-managed path cannot be the landing root")
            try:
                reserved.add(source_id(relative.parts[0]))
            except SourceIdError:
                continue
        return frozenset(reserved)

    @staticmethod
    def _require_real_directory(path: Path, *, label: str) -> None:
        try:
            mode = os.lstat(path).st_mode
        except OSError as exc:
            raise SourceRegistryError(f"Jeb cannot inspect {label}") from exc
        if os.path.islink(path):
            raise SourceRegistryError(f"{label} must not be a symlink")
        if not stat.S_ISDIR(mode):
            raise SourceRegistryError(f"{label} must be a directory")


__all__ = ["SourceRootResolver"]

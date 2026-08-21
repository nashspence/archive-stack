"""Bounded adapter-local export of one configured opaque object root."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from riverhog_storage_adapter_protocol import canonical_json_bytes, normalize_object_path

RECOVERY_EXPORT_FORMAT = "riverhog-storage-adapter-recovery-export/v1"


@dataclass(frozen=True, slots=True)
class RecoveryExportEntry:
    """One current provider object selected by an adapter-local export source."""

    object_path: str
    stored_bytes: int
    source_ref: str

    def __post_init__(self) -> None:
        normalize_object_path(self.object_path)
        if self.stored_bytes < 0:
            raise ValueError("recovery-export object bytes must be nonnegative")
        if not self.source_ref:
            raise ValueError("recovery-export source reference must not be empty")


class RecoveryExportSource(Protocol):
    """Adapter-local current-root source; entries are canonically path ordered."""

    def iter_recovery_export_entries(self) -> Iterator[RecoveryExportEntry]: ...

    def iter_recovery_export_content(
        self,
        entry: RecoveryExportEntry,
    ) -> Iterator[bytes]: ...


def export_recovery_root(
    source: RecoveryExportSource,
    destination: Path,
) -> dict[str, object]:
    """Stream one configured target root into a new empty recovery directory."""

    requested = destination.expanduser()
    if requested.is_symlink():
        raise ValueError("recovery-export destination must not be a symlink")
    root = requested.resolve(strict=False)
    if root.exists():
        if not root.is_dir():
            raise ValueError("recovery-export destination must be a directory")
        if next(root.iterdir(), None) is not None:
            raise ValueError("recovery-export destination must be empty")
    else:
        root.mkdir(mode=0o700, parents=True)
    object_count = 0
    stored_bytes = 0
    root_digest = hashlib.sha256()
    previous_path: str | None = None
    for entry in source.iter_recovery_export_entries():
        object_path = normalize_object_path(entry.object_path)
        if previous_path is not None and object_path <= previous_path:
            raise ValueError("recovery-export entries must be unique and canonically ordered")
        previous_path = object_path
        target = root.joinpath(*object_path.split("/"))
        _mkdir_confined(root, target.parent)
        if target.exists() or target.is_symlink():
            raise ValueError("recovery-export target already exists")
        temporary = target.with_name(f".{target.name}.riverhog-export-part")
        digest = hashlib.sha256()
        received = 0
        try:
            with temporary.open("xb") as handle:
                for chunk in source.iter_recovery_export_content(entry):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if received != entry.stored_bytes:
                raise ValueError("recovery-export object length differs from provider metadata")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        record = {
            "object_path": object_path,
            "stored_bytes": received,
            "stored_sha256": digest.hexdigest(),
        }
        root_digest.update(canonical_json_bytes(record))
        root_digest.update(b"\n")
        object_count += 1
        stored_bytes += received

    return {
        "format": RECOVERY_EXPORT_FORMAT,
        "objects": object_count,
        "stored_bytes": stored_bytes,
        "root_sha256": root_digest.hexdigest(),
    }


def recovery_export_main(
    source_factory: Callable[[], RecoveryExportSource],
    *,
    prog: str,
    version: str,
    argv: Sequence[str] | None = None,
) -> int:
    """Run the conventional adapter-local recovery export command."""

    parser = argparse.ArgumentParser(
        prog=prog,
        description=("Export the configured opaque object root for use with riverhog-recover."),
    )
    parser.add_argument("--version", action="version", version=version)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    report = export_recovery_root(source_factory(), args.destination)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _mkdir_confined(root: Path, directory: Path) -> None:
    relative = directory.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("recovery-export path contains a symlink")
        current.mkdir(mode=0o700, exist_ok=True)
        if not current.is_dir():
            raise ValueError("recovery-export path is not a directory")


__all__ = [
    "RECOVERY_EXPORT_FORMAT",
    "RecoveryExportEntry",
    "RecoveryExportSource",
    "export_recovery_root",
    "recovery_export_main",
]

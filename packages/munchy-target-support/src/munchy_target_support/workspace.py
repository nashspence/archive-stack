"""Custody-safe helpers for the shared-directory/v1 transport binding."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Literal

from munchy_target_support.protocol import (
    WORKSPACE_ID_PATTERN,
    Artifact,
    normalize_relative_posix_path,
)

WorkspaceArea = Literal["input", "output", "jobs"]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def workspace_area_root(root: Path, area: WorkspaceArea, workspace_id: str) -> Path:
    import re

    if re.fullmatch(WORKSPACE_ID_PATTERN, workspace_id) is None:
        raise ValueError("workspace_id is not a safe shared-directory ID")
    root = root.expanduser().resolve()
    candidate = root / area / workspace_id
    _reject_symlink_components(root, candidate)
    return candidate


def workspace_artifact_path(
    root: Path,
    area: WorkspaceArea,
    workspace_id: str,
    relative_path: str,
) -> Path:
    normalized = normalize_relative_posix_path(relative_path)
    area_root = workspace_area_root(root, area, workspace_id)
    candidate = area_root.joinpath(*PurePosixPath(normalized).parts)
    _reject_symlink_components(area_root, candidate)
    return candidate


def _reject_symlink_components(anchor: Path, candidate: Path) -> None:
    try:
        relative = candidate.relative_to(anchor)
    except ValueError as exc:
        raise ValueError("workspace path escapes its authority root") from exc
    current = anchor
    if current.exists() and current.is_symlink():
        raise ValueError(f"workspace path contains a symlink: {current}")
    for part in relative.parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise ValueError(f"workspace path contains a symlink: {current}")


def verify_artifact(
    root: Path,
    area: WorkspaceArea,
    workspace_id: str,
    artifact: Artifact,
) -> Path:
    path = workspace_artifact_path(root, area, workspace_id, artifact.path)
    try:
        before = path.stat(follow_symlinks=False)
    except FileNotFoundError as exc:
        raise ValueError(f"artifact is missing: {artifact.id}") from exc
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"artifact is not a regular file: {artifact.id}")
    if before.st_size != artifact.bytes:
        raise ValueError(f"artifact byte count does not match: {artifact.id}")
    digest = file_sha256(path)
    after = path.stat(follow_symlinks=False)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ValueError(f"artifact changed during verification: {artifact.id}")
    if digest != artifact.sha256:
        raise ValueError(f"artifact sha256 does not match: {artifact.id}")
    return path


def verify_artifacts(
    root: Path,
    area: WorkspaceArea,
    workspace_id: str,
    artifacts: tuple[Artifact, ...],
) -> dict[str, Path]:
    return {
        artifact.id: verify_artifact(root, area, workspace_id, artifact) for artifact in artifacts
    }


def publish_file_atomically(source: Path, destination: Path) -> None:
    """Publish a completed target output without exposing partial final content."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(destination.parent, destination)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.part")
    try:
        temporary.unlink(missing_ok=True)
        with source.open("rb") as reader, temporary.open("xb") as writer:
            while chunk := reader.read(1024 * 1024):
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = [
    "file_sha256",
    "publish_file_atomically",
    "verify_artifact",
    "verify_artifacts",
    "workspace_area_root",
    "workspace_artifact_path",
]

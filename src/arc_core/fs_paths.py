from __future__ import annotations

import shutil
from collections.abc import Iterable
from pathlib import Path, PurePosixPath


class PathNormalizationError(ValueError):
    pass


def normalize_relpath(raw: str) -> str:
    candidate = raw.strip().replace("\\", "/")
    if not candidate or candidate in {".", "/"}:
        raise PathNormalizationError("path must not be empty")
    path = PurePosixPath(candidate)
    if path.is_absolute():
        raise PathNormalizationError("path must be relative")
    parts: list[str] = []
    for part in path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            raise PathNormalizationError("path must not escape its root")
        parts.append(part)
    if not parts:
        raise PathNormalizationError("path must not be empty")
    return "/".join(parts)


def normalize_collection_id(raw: str) -> str:
    if not raw.strip():
        raise PathNormalizationError("collection id must not be empty")
    candidate = raw.replace("\\", "/")
    normalized = normalize_relpath(candidate)
    if raw != normalized:
        raise PathNormalizationError("collection id must be canonical")
    return normalized


def derive_collection_id_from_staging_path(staging_path: str) -> str:
    candidate = staging_path.strip().replace("\\", "/")
    path = PurePosixPath(candidate)
    if not path.is_absolute():
        raise PathNormalizationError("staging path must be absolute")
    parts = [part for part in path.parts if part != "/"]
    try:
        staging_index = parts.index("staging")
    except ValueError as exc:
        raise PathNormalizationError("staging path must include a staging root segment") from exc
    if len(parts) <= staging_index + 1:
        raise PathNormalizationError(
            "staging path must include a collection path beneath the staging root"
        )
    return normalize_collection_id("/".join(parts[staging_index + 1 :]))


def normalize_root_node_name(raw: str) -> str:
    normalized = normalize_collection_id(raw)
    if "/" in normalized:
        raise PathNormalizationError("root node name must be a single path segment")
    return normalized


def collection_id_ancestors(collection_id: str) -> list[str]:
    parts = normalize_collection_id(collection_id).split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def find_collection_id_conflict(existing_ids: Iterable[str], candidate: str) -> str | None:
    normalized_candidate = normalize_collection_id(candidate)
    normalized_existing = {normalize_collection_id(current) for current in existing_ids}

    for ancestor in collection_id_ancestors(normalized_candidate):
        if ancestor in normalized_existing:
            return ancestor

    prefix = f"{normalized_candidate}/"
    for existing in sorted(normalized_existing):
        if existing.startswith(prefix):
            return existing

    return None


def path_parents(relpath: str) -> list[str]:
    parts = normalize_relpath(relpath).split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def safe_remove_tree(path: Path) -> None:
    if path.exists() or path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)


def safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass

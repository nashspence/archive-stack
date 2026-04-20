from __future__ import annotations

import shutil
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



def normalize_root_node_name(raw: str) -> str:
    candidate = raw.strip()
    if not candidate:
        raise PathNormalizationError("root node name must not be empty")
    normalized = normalize_relpath(candidate)
    if "/" in normalized:
        raise PathNormalizationError("root node name must be a single path segment")
    if normalized in {".", ".."}:
        raise PathNormalizationError("root node name must not be . or ..")
    return normalized



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

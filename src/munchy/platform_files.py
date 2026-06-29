from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Sequence

DEFAULT_PLATFORM_CRUFT_EXCLUDES = (
    ".DS_Store",
    "**/.DS_Store",
    "._*",
    "**/._*",
    ".Spotlight-V100/**",
    "**/.Spotlight-V100/**",
    ".Trashes/**",
    "**/.Trashes/**",
    ".fseventsd/**",
    "**/.fseventsd/**",
)


def normalize_exclude_patterns(patterns: Iterable[str], *, label: str) -> list[str]:
    excludes: list[str] = []
    for item in patterns:
        pattern = item.strip()
        if not pattern:
            raise ValueError(f"{label} entries must not be blank")
        if pattern not in excludes:
            excludes.append(pattern)
    return excludes


def path_matches_exclude_patterns(rel_path: str, excludes: Sequence[str]) -> bool:
    rel = rel_path.strip("/")
    return any(fnmatch.fnmatchcase(rel, pattern) for pattern in excludes)


def is_platform_cruft_path(rel_path: str) -> bool:
    return path_matches_exclude_patterns(rel_path, DEFAULT_PLATFORM_CRUFT_EXCLUDES)

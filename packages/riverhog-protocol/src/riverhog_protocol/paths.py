from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath
from typing import Annotated

from pydantic import AfterValidator, Field

__all__ = [
    "CANONICAL_RELPATH_PATTERN",
    "MAX_TAG_LENGTH",
    "CanonicalRelPath",
    "CanonicalTag",
    "PathNormalizationError",
    "normalize_collection_id",
    "normalize_relpath",
    "normalize_tag",
    "validate_canonical_relpath",
    "validate_canonical_tag",
]


class PathNormalizationError(ValueError):
    pass


_TAG_SEPARATOR_RE = re.compile(r"[^a-z0-9]+")
MAX_TAG_LENGTH = 80
CANONICAL_RELPATH_PATTERN = r"^[^/\\]+(?:/[^/\\]+)*$"


def validate_canonical_relpath(value: str) -> str:
    normalized = normalize_relpath(value)
    if normalized != value:
        raise PathNormalizationError("path must be canonical")
    return normalized


type CanonicalRelPath = Annotated[
    str,
    Field(
        min_length=1,
        max_length=4096,
        pattern=CANONICAL_RELPATH_PATTERN,
        json_schema_extra={
            "format": "riverhog-canonical-relpath-v1",
            "allOf": [
                {"not": {"pattern": r"(?:^|/)\.{1,2}(?:/|$)"}},
                {"not": {"pattern": r"^\s|\s$"}},
            ],
        },
    ),
    AfterValidator(validate_canonical_relpath),
]


def validate_canonical_tag(value: str) -> str:
    normalized = normalize_tag(value)
    if normalized != value:
        raise PathNormalizationError("tag must be canonical")
    return normalized


type CanonicalTag = Annotated[
    str,
    Field(max_length=MAX_TAG_LENGTH, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$"),
    AfterValidator(validate_canonical_tag),
]


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


def normalize_tag(raw: str) -> str:
    ascii_text = (
        unicodedata.normalize("NFKD", raw.strip().casefold())
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    normalized = _TAG_SEPARATOR_RE.sub("-", ascii_text).strip("-")
    normalized = re.sub(r"-{2,}", "-", normalized)
    normalized = normalized[:MAX_TAG_LENGTH].strip("-")
    if not normalized:
        raise PathNormalizationError("tag must include at least one letter or digit")
    return normalized


def normalize_collection_id(raw: str | int) -> int:
    if isinstance(raw, bool):
        raise PathNormalizationError("collection id must be a positive integer")
    text = str(raw)
    if not text or not text.isascii() or not text.isdecimal():
        raise PathNormalizationError("collection id must be a positive integer")
    value = int(text)
    if value < 1 or text != str(value):
        raise PathNormalizationError("collection id must be a canonical positive integer")
    return value

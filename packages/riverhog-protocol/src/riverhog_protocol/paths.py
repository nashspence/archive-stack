from __future__ import annotations

import unicodedata
from pathlib import PurePosixPath
from typing import Annotated

from pydantic import AfterValidator, BeforeValidator, Field

__all__ = [
    "CANONICAL_RELPATH_PATTERN",
    "CanonicalRelPath",
    "CollectionId",
    "CollectionIdParameter",
    "PathNormalizationError",
    "normalize_collection_id",
    "normalize_relpath",
    "relpath_search_key",
    "relpath_sort_key",
    "text_search_key",
    "validate_canonical_relpath",
    "validate_collection_id",
]


class PathNormalizationError(ValueError):
    pass


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
            "x-unicode-normalization": "NFC",
            "allOf": [
                {"not": {"pattern": r"(?:^|/)\.{1,2}(?:/|$)"}},
                {"not": {"pattern": r"^\s|\s$"}},
            ],
        },
    ),
    AfterValidator(validate_canonical_relpath),
]


def normalize_relpath(raw: str) -> str:
    candidate = unicodedata.normalize("NFC", raw.strip()).replace("\\", "/")
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


def relpath_sort_key(value: str) -> bytes:
    """Return the database-independent canonical ordering key for one path."""

    return validate_canonical_relpath(value).encode("utf-8")


def relpath_search_key(value: str) -> str:
    """Return Riverhog's stable ASCII-insensitive search projection for one path.

    Non-ASCII path text remains exact. This deliberately avoids delegating
    semantic normalization to database or operating-system collation tables.
    """

    return text_search_key(validate_canonical_relpath(value))


def text_search_key(value: str) -> str:
    """Normalize free text with the same stable projection used for paths."""

    normalized = unicodedata.normalize("NFC", value)
    return normalized.translate(
        str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    )


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


def validate_collection_id(value: object) -> int:
    """Validate a JSON/Python collection identity without coercing its type."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PathNormalizationError("collection id must be a positive integer")
    return value


def parse_collection_id_parameter(value: object) -> int:
    """Parse one canonical positive collection identity from URL text."""

    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise PathNormalizationError("collection id must be a positive integer")
    return normalize_collection_id(value)


type CollectionId = Annotated[
    int,
    Field(ge=1),
    BeforeValidator(validate_collection_id),
]
type CollectionIdParameter = Annotated[
    int,
    Field(ge=1),
    BeforeValidator(parse_collection_id_parameter),
]

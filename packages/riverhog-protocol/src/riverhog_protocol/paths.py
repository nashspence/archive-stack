from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from pathlib import PurePosixPath

__all__ = [
    "MAX_UPLOAD_SLUG_LENGTH",
    "PathNormalizationError",
    "collection_id_for_upload",
    "normalize_collection_id",
    "normalize_relpath",
    "normalize_upload_slug",
    "normalize_upload_timestamp",
    "path_parents",
]


class PathNormalizationError(ValueError):
    pass


_SLUG_SEPARATOR_RE = re.compile(r"[^a-z0-9]+")
_UPLOAD_TIMESTAMP_RE = re.compile(r"^\d{8}T\d{6}Z$")
MAX_UPLOAD_SLUG_LENGTH = 80


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


def normalize_upload_slug(raw: str) -> str:
    ascii_text = (
        unicodedata.normalize("NFKD", raw.strip().casefold())
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    normalized = _SLUG_SEPARATOR_RE.sub("-", ascii_text).strip("-")
    normalized = re.sub(r"-{2,}", "-", normalized)
    normalized = normalized[:MAX_UPLOAD_SLUG_LENGTH].strip("-")
    if not normalized:
        raise PathNormalizationError(
            "collection upload slug must include at least one letter or digit"
        )
    return normalized


def normalize_upload_timestamp(raw: str) -> str:
    candidate = raw.strip()
    if not _UPLOAD_TIMESTAMP_RE.fullmatch(candidate):
        raise PathNormalizationError(
            "collection upload timestamp must use UTC basic form YYYYMMDDTHHMMSSZ"
        )
    try:
        datetime.strptime(candidate, "%Y%m%dT%H%M%SZ")
    except ValueError as exc:
        raise PathNormalizationError(
            "collection upload timestamp must be a valid UTC timestamp"
        ) from exc
    return candidate


def collection_id_for_upload(upload_slug: str, upload_timestamp: str) -> str:
    slug = normalize_upload_slug(upload_slug)
    timestamp = normalize_upload_timestamp(upload_timestamp)
    return f"{slug}/{timestamp}"


def normalize_collection_id(raw: str) -> str:
    if not raw.strip():
        raise PathNormalizationError("collection id must not be empty")
    normalized = normalize_relpath(raw.replace("\\", "/"))
    if raw != normalized:
        raise PathNormalizationError("collection id must be canonical")
    parts = normalized.split("/")
    if len(parts) != 2:
        raise PathNormalizationError("collection id must use slug/YYYYMMDDTHHMMSSZ")
    slug, timestamp = parts
    try:
        expected = collection_id_for_upload(slug, timestamp)
    except PathNormalizationError as exc:
        raise PathNormalizationError("collection id must use slug/YYYYMMDDTHHMMSSZ") from exc
    if normalized != expected:
        raise PathNormalizationError("collection id must use slug/YYYYMMDDTHHMMSSZ")
    return normalized


def path_parents(relpath: str) -> list[str]:
    parts = normalize_relpath(relpath).split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]

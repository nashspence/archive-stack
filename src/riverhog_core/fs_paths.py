from __future__ import annotations

import re
import shutil
import unicodedata
from datetime import datetime
from pathlib import Path, PurePosixPath


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
    return f"{timestamp[:4]}/{timestamp}__{slug}"


def normalize_collection_id(raw: str) -> str:
    if not raw.strip():
        raise PathNormalizationError("collection id must not be empty")
    normalized = normalize_relpath(raw.replace("\\", "/"))
    if raw != normalized:
        raise PathNormalizationError("collection id must be canonical")
    parts = normalized.split("/")
    if len(parts) != 2 or "__" not in parts[1]:
        raise PathNormalizationError("collection id must use YYYY/YYYYMMDDTHHMMSSZ__slug")
    year, leaf = parts
    timestamp, slug = leaf.split("__", 1)
    try:
        expected = collection_id_for_upload(slug, timestamp)
    except PathNormalizationError as exc:
        raise PathNormalizationError("collection id must use YYYY/YYYYMMDDTHHMMSSZ__slug") from exc
    if year != timestamp[:4] or normalized != expected:
        raise PathNormalizationError("collection id must use YYYY/YYYYMMDDTHHMMSSZ__slug")
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

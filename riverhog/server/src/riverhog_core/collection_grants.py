from __future__ import annotations

from collections.abc import Iterable

from riverhog_protocol.errors import BadRequest
from riverhog_protocol.paths import (
    PathNormalizationError,
    normalize_collection_id,
    normalize_upload_slug,
)

ALL_COLLECTIONS = "*"
COLLECTION_PREFIX = "collection:"
SLUG_PREFIX = "slug:"


def normalize_collection_grants(values: Iterable[str]) -> tuple[str, ...]:
    grants = tuple(sorted({_normalize_collection_grant(value) for value in values}))
    if ALL_COLLECTIONS in grants and len(grants) != 1:
        raise BadRequest("the all-collections grant must be used alone")
    return grants


def collection_slug(collection_id: str) -> str:
    try:
        return normalize_collection_id(collection_id).split("/", 1)[0]
    except PathNormalizationError as exc:
        raise BadRequest(str(exc)) from exc


def grant_allows_slug(grant: str, slug: str) -> bool:
    normalized_slug = _canonical_slug(slug)
    return grant == ALL_COLLECTIONS or grant == f"{SLUG_PREFIX}{normalized_slug}"


def grant_allows_collection(grant: str, collection_id: str) -> bool:
    try:
        normalized_collection = normalize_collection_id(collection_id)
    except PathNormalizationError as exc:
        raise BadRequest(str(exc)) from exc
    return (
        grant == ALL_COLLECTIONS
        or grant == f"{COLLECTION_PREFIX}{normalized_collection}"
        or grant == f"{SLUG_PREFIX}{normalized_collection.split('/', 1)[0]}"
    )


def grant_covers(grantor: str, requested: str) -> bool:
    if grantor == ALL_COLLECTIONS or grantor == requested:
        return True
    if grantor.startswith(SLUG_PREFIX) and requested.startswith(COLLECTION_PREFIX):
        return requested.removeprefix(COLLECTION_PREFIX).startswith(
            f"{grantor.removeprefix(SLUG_PREFIX)}/"
        )
    return False


def _normalize_collection_grant(value: str) -> str:
    candidate = str(value).strip()
    folded = candidate.casefold()
    if folded == ALL_COLLECTIONS:
        return ALL_COLLECTIONS
    if folded.startswith(SLUG_PREFIX):
        return f"{SLUG_PREFIX}{_canonical_slug(candidate[len(SLUG_PREFIX) :])}"
    if folded.startswith(COLLECTION_PREFIX):
        raw_collection = candidate[len(COLLECTION_PREFIX) :]
        try:
            collection_id = normalize_collection_id(raw_collection)
        except PathNormalizationError as exc:
            raise BadRequest(str(exc)) from exc
        return f"{COLLECTION_PREFIX}{collection_id}"
    raise BadRequest("collection grants must be *, slug:<slug>, or collection:<collection-id>")


def _canonical_slug(value: str) -> str:
    try:
        normalized = normalize_upload_slug(value)
    except PathNormalizationError as exc:
        raise BadRequest(str(exc)) from exc
    if value != normalized:
        raise BadRequest("collection grant slug must be canonical")
    return normalized


__all__ = [
    "ALL_COLLECTIONS",
    "COLLECTION_PREFIX",
    "SLUG_PREFIX",
    "collection_slug",
    "grant_allows_collection",
    "grant_allows_slug",
    "grant_covers",
    "normalize_collection_grants",
]

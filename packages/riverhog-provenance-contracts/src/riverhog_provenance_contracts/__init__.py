"""Canonical Riverhog provenance identity and reference contracts."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

CANONICAL_UUID_URN_PATTERN = (
    r"^urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def index_schema_documents(
    documents: Iterable[Mapping[str, Any]],
    *,
    owner: str,
) -> dict[str, dict[str, Any]]:
    """Index one contract pack without silently replacing a schema identity."""

    indexed: dict[str, dict[str, Any]] = {}
    for supplied in documents:
        document = dict(supplied)
        identifier = document.get("$id")
        if not isinstance(identifier, str) or not identifier or identifier != identifier.strip():
            raise ValueError(f"{owner} schema has no canonical $id")
        if identifier in indexed:
            raise ValueError(f"{owner} schema identity is duplicated: {identifier}")
        indexed[identifier] = document
    return dict(sorted(indexed.items()))


def require_canonical_uuid_urn(value: str, field: str = "identity") -> str:
    """Return one exact lowercase UUID URN or reject it."""

    prefix = "urn:uuid:"
    if not value.startswith(prefix):
        raise ValueError(f"{field} must be a lowercase UUID URN")
    try:
        parsed = uuid.UUID(value[len(prefix) :])
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid UUID URN") from exc
    canonical = f"urn:uuid:{parsed}"
    if value != canonical:
        raise ValueError(f"{field} must use canonical lowercase UUID URN syntax")
    return canonical


def _journal_id(value: str) -> str:
    return require_canonical_uuid_urn(value, "journal_id")


def _state_id(value: str) -> str:
    return require_canonical_uuid_urn(value, "current_state_id")


def _entry_id(value: str) -> str:
    return require_canonical_uuid_urn(value, "entry_id")


type ProvenanceJournalId = Annotated[
    str,
    Field(pattern=CANONICAL_UUID_URN_PATTERN),
    AfterValidator(_journal_id),
]
type ProvenanceStateId = Annotated[
    str,
    Field(pattern=CANONICAL_UUID_URN_PATTERN),
    AfterValidator(_state_id),
]
type ProvenanceEntryId = Annotated[
    str,
    Field(pattern=CANONICAL_UUID_URN_PATTERN),
    AfterValidator(_entry_id),
]


class ProvenanceJournalStateReference(BaseModel):
    """One exact current state in one Riverhog provenance journal."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    journal_id: ProvenanceJournalId
    current_state_id: ProvenanceStateId


__all__ = [
    "CANONICAL_UUID_URN_PATTERN",
    "ProvenanceJournalId",
    "ProvenanceEntryId",
    "ProvenanceJournalStateReference",
    "ProvenanceStateId",
    "index_schema_documents",
    "require_canonical_uuid_urn",
]

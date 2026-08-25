"""Canonical Riverhog provenance identity and reference contracts."""

from __future__ import annotations

import uuid
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

CANONICAL_UUID_URN_PATTERN = (
    r"^urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


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


class ProvenanceJournalStateReference(BaseModel):
    """One exact current state in one Riverhog provenance journal."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    journal_id: ProvenanceJournalId
    current_state_id: ProvenanceStateId


__all__ = [
    "CANONICAL_UUID_URN_PATTERN",
    "ProvenanceJournalId",
    "ProvenanceJournalStateReference",
    "ProvenanceStateId",
    "require_canonical_uuid_urn",
]

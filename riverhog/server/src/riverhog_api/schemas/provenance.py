from __future__ import annotations

from typing import Literal

from pydantic import Field
from riverhog_protocol import (
    CollectionId,
    FileProvenanceBinding,
    ProvenanceSort,
    ProvenanceStatus,
    SortOrder,
)
from riverhog_protocol.paths import CanonicalRelPath
from riverhog_provenance_contracts import (
    ProvenanceEntryId,
    ProvenanceJournalId,
    ProvenanceStateId,
)

from riverhog_api.schemas.common import RiverhogModel


class ProvenanceJournalOut(RiverhogModel):
    journal_id: ProvenanceJournalId
    bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    entries: int = Field(ge=1)
    current_state_id: ProvenanceStateId
    current_path: CanonicalRelPath
    current_bytes: int = Field(ge=0)
    current_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    agent_ids: list[str]
    ancestor_journal_ids: list[ProvenanceJournalId]
    entity_counts: dict[str, int]


class CollectionFileProvenanceOut(RiverhogModel):
    collection_id: CollectionId
    path: CanonicalRelPath
    bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance: FileProvenanceBinding


class CollectionFileProvenanceDetailOut(CollectionFileProvenanceOut):
    journal: ProvenanceJournalOut | None = None


class ProvenanceExternalStateReferenceOut(RiverhogModel):
    from_journal_id: ProvenanceJournalId
    to_journal_id: ProvenanceJournalId
    state_id: ProvenanceStateId
    entry_id: ProvenanceEntryId
    entry_json_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CollectionFileProvenanceTraceOut(CollectionFileProvenanceDetailOut):
    journals: list[ProvenanceJournalOut]
    external_state_references: list[ProvenanceExternalStateReferenceOut]


class ListCollectionFileProvenanceResponse(RiverhogModel):
    page: int = Field(ge=1)
    per_page: int = Field(ge=0)
    total: int = Field(ge=0)
    pages: int = Field(ge=0)
    sort: ProvenanceSort
    order: SortOrder
    query: str | None
    status: ProvenanceStatus | None
    collection_id: CollectionId
    provenance_mode: Literal["captured", "mixed", "omitted"]
    provenance_identity: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    files: list[CollectionFileProvenanceOut]


class CollectionProvenanceVerificationOut(RiverhogModel):
    collection_id: CollectionId
    valid: Literal[True]
    provenance_mode: Literal["captured", "mixed", "omitted"]
    provenance_identity: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    files: int = Field(ge=0)
    journals: int = Field(ge=0)
    entities: int = Field(ge=0)

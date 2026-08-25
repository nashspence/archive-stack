from __future__ import annotations

from typing import Literal

from pydantic import Field
from riverhog_protocol import FileProvenanceBinding, ProvenanceSort, ProvenanceStatus, SortOrder

from riverhog_api.schemas.common import RiverhogModel


class ProvenanceJournalOut(RiverhogModel):
    journal_id: str
    bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    entries: int = Field(ge=1)
    current_state_id: str
    current_path: str
    current_bytes: int = Field(ge=0)
    current_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    agent_ids: list[str]
    ancestor_journal_ids: list[str]
    entity_counts: dict[str, int]


class CollectionFileProvenanceOut(RiverhogModel):
    collection_id: int
    path: str
    bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance: FileProvenanceBinding


class CollectionFileProvenanceDetailOut(CollectionFileProvenanceOut):
    journal: ProvenanceJournalOut | None = None


class ProvenanceExternalStateReferenceOut(RiverhogModel):
    from_journal_id: str
    to_journal_id: str
    state_id: str
    entry_id: str
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
    collection_id: int
    provenance_mode: Literal["captured", "mixed", "omitted"]
    provenance_identity: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    files: list[CollectionFileProvenanceOut]


class CollectionProvenanceVerificationOut(RiverhogModel):
    collection_id: int
    valid: Literal[True]
    provenance_mode: Literal["captured", "mixed", "omitted"]
    provenance_identity: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    files: int = Field(ge=0)
    journals: int = Field(ge=0)
    entities: int = Field(ge=0)

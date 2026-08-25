from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, RootModel
from riverhog_protocol import (
    CapturedFileProvenanceBinding,
    CollectionId,
    ImmutableFileIdentityDocument,
    OmittedFileProvenanceBinding,
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

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ProvenanceJournalOut(RiverhogModel):
    journal_id: ProvenanceJournalId
    bytes: int = Field(ge=0)
    sha256: Sha256
    entries: int = Field(ge=1)
    current_state_id: ProvenanceStateId
    current_path: CanonicalRelPath
    current_bytes: int = Field(ge=0)
    current_sha256: Sha256
    agent_ids: list[str]
    ancestor_journal_ids: list[ProvenanceJournalId]
    entity_counts: dict[str, int]


class CapturedCollectionFileProvenanceOut(ImmutableFileIdentityDocument):
    collection_id: CollectionId
    provenance: CapturedFileProvenanceBinding


class OmittedCollectionFileProvenanceOut(ImmutableFileIdentityDocument):
    collection_id: CollectionId
    provenance: OmittedFileProvenanceBinding


type _FileProvenanceOut = CapturedCollectionFileProvenanceOut | OmittedCollectionFileProvenanceOut


class CollectionFileProvenanceOut(RootModel[_FileProvenanceOut]):
    pass


class CapturedCollectionFileProvenanceDetailOut(CapturedCollectionFileProvenanceOut):
    journal: ProvenanceJournalOut


class OmittedCollectionFileProvenanceDetailOut(OmittedCollectionFileProvenanceOut):
    journal: None = None


class CollectionFileProvenanceDetailOut(
    RootModel[CapturedCollectionFileProvenanceDetailOut | OmittedCollectionFileProvenanceDetailOut]
):
    pass


class ProvenanceExternalStateReferenceOut(RiverhogModel):
    from_journal_id: ProvenanceJournalId
    to_journal_id: ProvenanceJournalId
    state_id: ProvenanceStateId
    entry_id: ProvenanceEntryId
    entry_json_sha256: Sha256


class CapturedCollectionFileProvenanceTraceOut(CapturedCollectionFileProvenanceDetailOut):
    journals: list[ProvenanceJournalOut] = Field(min_length=1)
    external_state_references: list[ProvenanceExternalStateReferenceOut]


class OmittedCollectionFileProvenanceTraceOut(OmittedCollectionFileProvenanceDetailOut):
    journals: list[ProvenanceJournalOut] = Field(default_factory=list, max_length=0)
    external_state_references: list[ProvenanceExternalStateReferenceOut] = Field(
        default_factory=list,
        max_length=0,
    )


class CollectionFileProvenanceTraceOut(
    RootModel[CapturedCollectionFileProvenanceTraceOut | OmittedCollectionFileProvenanceTraceOut]
):
    pass


class _CollectionFileProvenancePage(RiverhogModel):
    page: int = Field(ge=1)
    per_page: int = Field(ge=0)
    total: int = Field(ge=0)
    pages: int = Field(ge=0)
    sort: ProvenanceSort
    order: SortOrder
    query: str | None
    status: ProvenanceStatus | None
    collection_id: CollectionId


class CapturedCollectionFileProvenancePage(_CollectionFileProvenancePage):
    provenance_mode: Literal["captured"]
    provenance_identity: Sha256
    files: list[CapturedCollectionFileProvenanceOut]


class MixedCollectionFileProvenancePage(_CollectionFileProvenancePage):
    provenance_mode: Literal["mixed"]
    provenance_identity: Sha256
    files: list[_FileProvenanceOut]


class OmittedCollectionFileProvenancePage(_CollectionFileProvenancePage):
    provenance_mode: Literal["omitted"]
    provenance_identity: None
    files: list[OmittedCollectionFileProvenanceOut]


class ListCollectionFileProvenanceResponse(
    RootModel[
        Annotated[
            CapturedCollectionFileProvenancePage
            | MixedCollectionFileProvenancePage
            | OmittedCollectionFileProvenancePage,
            Field(discriminator="provenance_mode"),
        ]
    ]
):
    pass


class _CollectionProvenanceVerification(RiverhogModel):
    collection_id: CollectionId
    valid: Literal[True]
    files: int = Field(ge=0)
    entities: int = Field(ge=0)


class CapturedCollectionProvenanceVerification(_CollectionProvenanceVerification):
    provenance_mode: Literal["captured", "mixed"]
    provenance_identity: Sha256
    journals: int = Field(ge=1)


class OmittedCollectionProvenanceVerification(_CollectionProvenanceVerification):
    provenance_mode: Literal["omitted"]
    provenance_identity: None
    journals: Literal[0]
    entities: Literal[0]


class CollectionProvenanceVerificationOut(
    RootModel[
        Annotated[
            CapturedCollectionProvenanceVerification | OmittedCollectionProvenanceVerification,
            Field(discriminator="provenance_mode"),
        ]
    ]
):
    pass

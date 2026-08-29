from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, RootModel, model_validator
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
    agent_count: int = Field(ge=0)
    entity_counts: dict[str, int]


class ProvenanceJournalAgentOut(RiverhogModel):
    agent_id: str


class ListProvenanceJournalAgentsResponse(RiverhogModel):
    collection_id: CollectionId
    journal_id: ProvenanceJournalId
    page: int = Field(ge=1)
    per_page: int = Field(ge=1)
    total: int = Field(ge=0)
    pages: int = Field(ge=0)
    agents: list[ProvenanceJournalAgentOut]


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


class ProvenanceTraceJournalItemOut(RiverhogModel):
    kind: Literal["journal"]
    journal: ProvenanceJournalOut


class ProvenanceTraceExternalStateReferenceItemOut(RiverhogModel):
    kind: Literal["external_state_reference"]
    reference: ProvenanceExternalStateReferenceOut


type ProvenanceTraceItemOut = Annotated[
    ProvenanceTraceJournalItemOut | ProvenanceTraceExternalStateReferenceItemOut,
    Field(discriminator="kind"),
]


class _CollectionFileProvenanceTracePage(RiverhogModel):
    page: int = Field(ge=1)
    per_page: int = Field(ge=1)
    total: int = Field(ge=0)
    pages: int = Field(ge=0)
    items: list[ProvenanceTraceItemOut]


class CapturedCollectionFileProvenanceTraceOut(
    CapturedCollectionFileProvenanceDetailOut,
    _CollectionFileProvenanceTracePage,
):
    pass


class OmittedCollectionFileProvenanceTraceOut(
    OmittedCollectionFileProvenanceDetailOut,
    _CollectionFileProvenanceTracePage,
):
    pass


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


class CollectionProvenanceVerificationJobOut(RiverhogModel):
    collection_id: CollectionId
    state: Literal["queued", "running", "canceling", "succeeded", "failed", "canceled"]
    requested_at: str
    started_at: str | None
    finished_at: str | None
    attempts: int = Field(ge=0)
    result: CollectionProvenanceVerificationOut | None
    failure: str | None

    @model_validator(mode="after")
    def validate_terminal_evidence(self) -> CollectionProvenanceVerificationJobOut:
        if self.state == "succeeded":
            if self.result is None or self.failure is not None or self.finished_at is None:
                raise ValueError("succeeded provenance verification requires exact result evidence")
        elif self.state == "failed":
            if self.result is not None or not self.failure or self.finished_at is None:
                raise ValueError("failed provenance verification requires failure evidence")
        elif self.state == "canceled":
            if self.result is not None or self.finished_at is None:
                raise ValueError("canceled provenance verification requires terminal evidence")
        elif self.result is not None or self.finished_at is not None:
            raise ValueError("nonterminal provenance verification cannot contain terminal evidence")
        return self

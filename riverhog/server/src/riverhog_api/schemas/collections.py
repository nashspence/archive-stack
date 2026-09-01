from __future__ import annotations

from typing import Annotated, Any, Literal, cast

from http_api_contracts import BrowsePageToken, CanonicalVisibleText
from lifecycle_events import EventContext
from pydantic import ConfigDict, Field, field_validator, model_validator
from riverhog_protocol import (
    ArchiveStoreName,
    CollectionId,
    CollectionSort,
    CollectionUploadArtifactCustodyReceiptDocument,
    CollectionUploadCustodyMode,
    CollectionUploadFileBatchDocument,
    CollectionUploadProvenanceJournalStatusDocument,
    CollectionUploadRegistrationConstraintsDocument,
    CollectionUploadSort,
    CollectionUploadState,
    CollectionUploadUnitWorkDocument,
    CollectionUploadVolumeSummaryDocument,
    CollectionUploadWorkBatchDocument,
    FileProvenanceBinding,
    ImmutableFileIdentityDocument,
    ProcessingClaimId,
    RetirementClaimReferenceDocument,
    SortOrder,
    validate_collection_upload_artifact_custody_receipt,
)
from riverhog_protocol import (
    CollectionUploadFileIn as CollectionUploadFileIn,
)
from riverhog_protocol.paths import CanonicalTag
from riverhog_protocol.transport import COLLECTION_DELETION_BLOCKERS_MAX
from time_formats import format_utc_timestamp, parse_utc_timestamp

from riverhog_api.schemas.archive import ArchiveCopyOut
from riverhog_api.schemas.common import RiverhogModel

_UPLOAD_NONCUSTODY_STATES = ["uploading", "finalizing", "canceled", "finalized"]
_UPLOAD_LEASE_STATES = ["open", "closing"]
_UPLOAD_CUSTODY_STATE_SCHEMA: list[dict[str, Any]] = [
    {
        "if": {
            "properties": {"custody_mode": {"const": "producer-retained"}},
            "required": ["custody_mode"],
        },
        "then": {
            "properties": {
                "state": {"enum": ["open", "uploading", "finalizing", "canceled", "finalized"]},
                "upload_state_expires_at": {"type": "null"},
                "orphaned_at": {"type": "null"},
            }
        },
    },
    {
        "if": {
            "properties": {"state": {"enum": ["orphaned", "discarding"]}},
            "required": ["state"],
        },
        "then": {
            "properties": {
                "custody_mode": {"const": "custody-transfer"},
                "upload_state_expires_at": {"type": "null"},
                "orphaned_at": {"type": "string"},
            }
        },
    },
    {
        "if": {
            "properties": {
                "custody_mode": {"const": "custody-transfer"},
                "state": {"enum": _UPLOAD_LEASE_STATES},
            },
            "required": ["custody_mode", "state"],
        },
        "then": {
            "properties": {
                "upload_state_expires_at": {"type": "string"},
                "orphaned_at": {"type": "null"},
            }
        },
    },
    {
        "if": {
            "properties": {
                "custody_mode": {"const": "custody-transfer"},
                "state": {"enum": _UPLOAD_NONCUSTODY_STATES},
            },
            "required": ["custody_mode", "state"],
        },
        "then": {
            "properties": {
                "upload_state_expires_at": {"type": "null"},
                "orphaned_at": {"type": "null"},
            }
        },
    },
    {
        "if": {
            "properties": {"state": {"enum": ["finalizing", "finalized"]}},
            "required": ["state"],
        },
        "then": {
            "properties": {
                "custody": {
                    "properties": {"state": {"const": "complete"}},
                    "required": ["state"],
                }
            }
        },
    },
    {
        "if": {
            "properties": {
                "custody_mode": {"const": "custody-transfer"},
                "state": {"const": "uploading"},
            },
            "required": ["custody_mode", "state"],
        },
        "then": {
            "properties": {
                "custody": {
                    "properties": {"state": {"const": "complete"}},
                    "required": ["state"],
                }
            }
        },
    },
]
CollectionUploadArchivePhase = Literal[
    "planning",
    "uploading",
    "finalization_queued",
    "finalizing",
    "retry_wait",
    "completed",
    "canceled",
    "orphaned",
    "discarding",
]
_CANONICAL_UTC_TIMESTAMP_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"
_UPLOAD_ARCHIVE_STATE_PHASES: dict[str, tuple[str, ...]] = {
    "open": ("planning", "uploading"),
    "closing": ("uploading",),
    "uploading": ("uploading",),
    "finalizing": ("finalization_queued", "finalizing", "retry_wait"),
    "finalized": ("completed",),
    "canceled": ("canceled",),
    "orphaned": ("orphaned",),
    "discarding": ("discarding",),
}
_UPLOAD_ARCHIVE_PHASE_SCHEMA: list[dict[str, Any]] = [
    *(
        {
            "if": {
                "properties": {"state": {"const": state}},
                "required": ["state"],
            },
            "then": {
                "properties": {"archive_phase": {"enum": list(phases)}},
                "required": ["archive_phase"],
            },
        }
        for state, phases in _UPLOAD_ARCHIVE_STATE_PHASES.items()
    ),
    {
        "if": {
            "properties": {"archive_phase": {"const": "retry_wait"}},
            "required": ["archive_phase"],
        },
        "then": {
            "properties": {
                "latest_failure": {"type": "string", "minLength": 1},
                "archive_next_attempt_at": {"type": "string", "minLength": 1},
            }
        },
    },
    {
        "if": {
            "properties": {"archive_phase": {"const": "finalization_queued"}},
            "required": ["archive_phase"],
        },
        "then": {
            "properties": {
                "latest_failure": {"type": "null"},
                "archive_next_attempt_at": {"type": "string", "minLength": 1},
            }
        },
    },
    {
        "if": {
            "properties": {
                "archive_phase": {"not": {"enum": ["finalization_queued", "retry_wait"]}}
            },
            "required": ["archive_phase"],
        },
        "then": {"properties": {"archive_next_attempt_at": {"type": "null"}}},
    },
]
_UPLOAD_PROVENANCE_STATE_SCHEMA: list[dict[str, Any]] = [
    {
        "if": {
            "properties": {"state": {"const": "finalized"}},
            "required": ["state"],
        },
        "then": {
            "oneOf": [
                {
                    "properties": {
                        "provenance_mode": {"enum": ["captured", "mixed"]},
                        "provenance_identity": {
                            "type": "string",
                            "pattern": r"^[0-9a-f]{64}$",
                        },
                    },
                    "required": ["provenance_mode", "provenance_identity"],
                },
                {
                    "properties": {
                        "provenance_mode": {"const": "omitted"},
                        "provenance_identity": {"type": "null"},
                    },
                    "required": ["provenance_mode", "provenance_identity"],
                },
            ]
        },
        "else": {
            "properties": {
                "provenance_mode": {"enum": ["captured", "omitted"]},
                "provenance_identity": {"type": "null"},
            }
        },
    }
]


def _canonical_timestamp(value: str) -> str:
    try:
        parsed = parse_utc_timestamp(value)
    except ValueError as exc:
        raise ValueError("timestamp must include UTC context") from exc
    if format_utc_timestamp(parsed) != value:
        raise ValueError("timestamp must use the canonical UTC representation")
    return value


def _validate_upload_custody_state(
    *,
    state: str,
    custody_mode: str,
    upload_state_expires_at: str | None,
    orphaned_at: str | None,
) -> None:
    if state in _UPLOAD_NONCUSTODY_STATES:
        if upload_state_expires_at is not None or orphaned_at is not None:
            raise ValueError("noncustody upload states cannot retain custody lease state")
        return
    if custody_mode == "producer-retained":
        if state in {"closing", "orphaned", "discarding"}:
            raise ValueError(
                "producer-retained upload sessions cannot enter custody-transfer states"
            )
        if upload_state_expires_at is not None or orphaned_at is not None:
            raise ValueError("producer-retained upload sessions cannot have custody lease state")
        return
    if state in {"orphaned", "discarding"}:
        if upload_state_expires_at is not None or orphaned_at is None:
            raise ValueError("orphan custody state requires its transition time and no lease")
        return
    if (
        state not in _UPLOAD_LEASE_STATES
        or upload_state_expires_at is None
        or orphaned_at is not None
    ):
        raise ValueError("active custody-transfer state requires its lease and no orphan time")


def _validate_complete_upload_custody(
    *,
    state: str,
    custody_mode: str | None,
    custody_state: str,
) -> None:
    complete_required = state in {"finalizing", "finalized"} or (
        state == "uploading" and custody_mode == "custody-transfer"
    )
    if complete_required and custody_state != "complete":
        raise ValueError(f"{state} upload state requires complete Riverhog custody")


class PendingCollectionUploadCustodyOut(RiverhogModel):
    state: Literal["pending"]
    files: int = Field(ge=0, strict=True)
    bytes: int = Field(ge=0, strict=True)


class CompleteCollectionUploadCustodyOut(RiverhogModel):
    state: Literal["complete"]


CollectionUploadCustodyOut = Annotated[
    PendingCollectionUploadCustodyOut | CompleteCollectionUploadCustodyOut,
    Field(discriminator="state"),
]


def _validate_upload_custody_progress(
    *,
    files: int,
    bytes: int,
    custody: PendingCollectionUploadCustodyOut | CompleteCollectionUploadCustodyOut,
) -> None:
    if not isinstance(custody, PendingCollectionUploadCustodyOut):
        return
    if custody.files > files or custody.bytes > bytes:
        raise ValueError("upload custody progress cannot exceed collection totals")
    if custody.files == 0 and custody.bytes != 0:
        raise ValueError("upload custody bytes require at least one custodied file")
    if (custody.files, custody.bytes) == (files, bytes):
        raise ValueError("complete upload custody cannot be represented as pending progress")


class CreateOrResumeCollectionUploadSessionRequest(RiverhogModel):
    model_config = ConfigDict(
        json_schema_extra={
            "oneOf": [
                {
                    "properties": {
                        "provenance_mode": {"const": "captured"},
                        "provenance_omission_reason": {"type": "null"},
                    }
                },
                {
                    "properties": {
                        "provenance_mode": {"const": "omitted"},
                        "provenance_omission_reason": {"type": "string"},
                    },
                    "required": ["provenance_mode", "provenance_omission_reason"],
                },
            ]
        }
    )

    idempotency_key: CanonicalVisibleText = Field(max_length=200)
    initial_tag: CanonicalTag | None = None
    tag_set_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    ingest_source: str | None = None
    archive_store: ArchiveStoreName | None = None
    event_context: EventContext | None = None
    provenance_mode: Literal["captured", "omitted"] = "captured"
    provenance_omission_reason: CanonicalVisibleText | None = None
    custody_mode: CollectionUploadCustodyMode = "producer-retained"

    @model_validator(mode="after")
    def validate_provenance_choice(self) -> CreateOrResumeCollectionUploadSessionRequest:
        if self.provenance_mode == "captured":
            if self.provenance_omission_reason is not None:
                raise ValueError("captured provenance cannot have an omission reason")
            return self
        reason = self.provenance_omission_reason
        if reason is None or not reason or reason.strip() != reason:
            raise ValueError("omitted provenance requires a canonical omission reason")
        return self


class RegisterCollectionUploadSessionFilesRequest(CollectionUploadFileBatchDocument):
    pass


class CompleteCollectionUploadSessionRequest(RiverhogModel):
    files_total: int = Field(ge=1, strict=True)
    content_identity: str = Field(pattern=r"^[0-9a-f]{64}$")


class CollectionSummaryOut(RiverhogModel):
    id: CollectionId
    created_at: str
    tag_count: int = Field(ge=0, strict=True)
    content_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    tag_set_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    archive_root_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    encryption_format: str
    passphrase_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    files: int
    bytes: int
    remote_storage_bytes: int
    archive_copy_count: int = Field(ge=0, strict=True)


class ListCollectionsResponse(RiverhogModel):
    page_size: int = Field(ge=1, le=100)
    next_page_token: BrowsePageToken | None
    sort: CollectionSort
    order: SortOrder
    query: str | None
    tag: CanonicalTag | None
    encryption_format: str | None
    passphrase_id: str | None
    collections: list[CollectionSummaryOut]


class CollectionArchiveCopyListOut(RiverhogModel):
    collection_id: CollectionId
    page_size: int = Field(ge=1, le=100, strict=True)
    next_page_token: BrowsePageToken | None
    copies: list[ArchiveCopyOut]


class CollectionDeletionArchiveCopyOut(RiverhogModel):
    store: ArchiveStoreName
    objects: int
    stored_bytes: int


class CollectionDeletionPlanOut(RiverhogModel):
    model_config = ConfigDict(
        json_schema_extra={
            "oneOf": [
                {
                    "properties": {
                        "status": {"const": "blocked"},
                        "challenge": {"type": "null"},
                        "blockers": {"minItems": 1},
                    }
                },
                {
                    "properties": {
                        "status": {"enum": ["ready", "deleting"]},
                        "challenge": {"type": "string", "minLength": 1},
                        "blockers": {"maxItems": 0},
                    }
                },
            ]
        }
    )

    status: Literal["ready", "blocked", "deleting"]
    collection_id: CollectionId
    warning: str
    expires_at: str
    challenge: str | None
    file_count: int
    bytes: int
    archive_copies: list[CollectionDeletionArchiveCopyOut]
    archive_object_count: int
    remote_storage_bytes: int
    upload_file_count: int
    inventory_identity: str
    metadata_rows: dict[str, int]
    retirement_claim: RetirementClaimReferenceDocument | None = None
    blockers: list[str] = Field(
        max_length=COLLECTION_DELETION_BLOCKERS_MAX,
        json_schema_extra={
            "x-riverhog-extent": {
                "policy": "contract_max",
                "reason": "bounded-diagnostic-sample-with-explicit-overflow-markers",
            }
        },
    )
    billing_note: str

    @model_validator(mode="after")
    def validate_plan_state(self) -> CollectionDeletionPlanOut:
        if self.status == "blocked":
            if self.challenge is not None or not self.blockers:
                raise ValueError("blocked collection deletion requires blockers and no challenge")
        elif not self.challenge or self.blockers:
            raise ValueError("ready collection deletion requires a challenge and no blockers")
        return self


class DeleteCollectionRequest(RiverhogModel):
    challenge: str
    retirement_claim_id: ProcessingClaimId | None = None
    event_context: EventContext | None = None


class CollectionDeletionResultOut(RiverhogModel):
    status: Literal["deleted", "already_absent"]
    collection_id: CollectionId
    files: int
    bytes: int
    remote_storage_bytes: int


class CollectionUploadFileOut(ImmutableFileIdentityDocument):
    provenance: FileProvenanceBinding | None = None
    custody_receipt: CollectionUploadArtifactCustodyReceiptDocument | None = None


CollectionUploadProvenanceJournalOut = CollectionUploadProvenanceJournalStatusDocument


class CollectionUploadSessionFilesRegistrationOut(RiverhogModel):
    collection_id: CollectionId
    ingest_source: str | None
    archive_store: ArchiveStoreName
    encryption_format: str
    passphrase_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    state: Literal["open"]
    files: list[CollectionUploadFileOut]
    volumes: list[CollectionUploadVolumeSummaryOut]

    @model_validator(mode="after")
    def validate_custody_receipts(self) -> CollectionUploadSessionFilesRegistrationOut:
        for item in self.files:
            if item.custody_receipt is not None:
                validate_collection_upload_artifact_custody_receipt(
                    self.collection_id,
                    item,
                    item.custody_receipt,
                )
        sequences = [item.sequence for item in self.volumes]
        identities = [item.volume_id for item in self.volumes]
        if sequences != sorted(sequences) or len(identities) != len(set(identities)):
            raise ValueError("registered upload volumes must be unique and ordered")
        return self


CollectionUploadVolumeSummaryOut = CollectionUploadVolumeSummaryDocument


class ListCollectionUploadSessionFilesResponse(RiverhogModel):
    collection_id: CollectionId
    page_size: int = Field(ge=1, le=100)
    next_page_token: BrowsePageToken | None
    files: list[CollectionUploadFileOut]

    @model_validator(mode="after")
    def validate_custody_receipts(self) -> ListCollectionUploadSessionFilesResponse:
        for item in self.files:
            if item.custody_receipt is not None:
                validate_collection_upload_artifact_custody_receipt(
                    self.collection_id,
                    item,
                    item.custody_receipt,
                )
        return self


class CollectionUploadTagOut(RiverhogModel):
    tag: CanonicalTag


class ListCollectionUploadSessionTagsResponse(RiverhogModel):
    collection_id: CollectionId
    page_size: int = Field(ge=1, le=100, strict=True)
    next_page_token: BrowsePageToken | None
    tags: list[CollectionUploadTagOut]


class CollectionUploadTagMutationOut(RiverhogModel):
    collection_id: CollectionId
    tag_count: int = Field(ge=0, strict=True)


class CollectionUploadListItemOut(RiverhogModel):
    model_config = ConfigDict(json_schema_extra={"allOf": cast(Any, _UPLOAD_CUSTODY_STATE_SCHEMA)})

    collection_id: CollectionId
    created_at: str | None
    tag_count: int = Field(ge=0, strict=True)
    ingest_source: str | None
    archive_store: ArchiveStoreName
    encryption_format: str
    passphrase_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    state: Literal["open", "closing", "uploading", "finalizing", "orphaned", "discarding"]
    custody_mode: CollectionUploadCustodyMode
    files: int = Field(ge=0, strict=True)
    bytes: int = Field(ge=0, strict=True)
    custody: CollectionUploadCustodyOut
    upload_state_expires_at: str | None
    orphaned_at: str | None

    @model_validator(mode="after")
    def validate_custody_state(self) -> CollectionUploadListItemOut:
        _validate_upload_custody_state(
            state=self.state,
            custody_mode=self.custody_mode,
            upload_state_expires_at=self.upload_state_expires_at,
            orphaned_at=self.orphaned_at,
        )
        _validate_complete_upload_custody(
            state=self.state,
            custody_mode=self.custody_mode,
            custody_state=self.custody.state,
        )
        _validate_upload_custody_progress(
            files=self.files,
            bytes=self.bytes,
            custody=self.custody,
        )
        return self


class CollectionUploadListFiltersOut(RiverhogModel):
    tag: CanonicalTag | None
    state: CollectionUploadState | None


class ListCollectionUploadSessionsResponse(RiverhogModel):
    page_size: int = Field(ge=1, le=100)
    next_page_token: BrowsePageToken | None
    sort: CollectionUploadSort
    order: SortOrder
    query: str | None
    filters: CollectionUploadListFiltersOut
    uploads: list[CollectionUploadListItemOut]


class CollectionUploadRegistrationConstraintsOut(CollectionUploadRegistrationConstraintsDocument):
    pass


class CollectionUploadSessionOut(RiverhogModel):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"state": {"const": "finalized"}}},
                    "then": {
                        "properties": {
                            "content_identity": {"type": "string"},
                            "archive_root_sha256": {"type": "string"},
                            "registration_constraints": {"type": "null"},
                            "collection": {"type": "object"},
                        }
                    },
                    "else": {
                        "properties": {
                            "content_identity": {"type": "null"},
                            "archive_root_sha256": {"type": "null"},
                            "registration_constraints": {"type": "object"},
                            "collection": {"type": "null"},
                        }
                    },
                },
                *_UPLOAD_PROVENANCE_STATE_SCHEMA,
                *_UPLOAD_CUSTODY_STATE_SCHEMA,
                *_UPLOAD_ARCHIVE_PHASE_SCHEMA,
            ]
        }
    )

    collection_id: CollectionId
    created_at: str
    tag_count: int = Field(ge=0, strict=True)
    ingest_source: str | None
    provenance_mode: Literal["captured", "mixed", "omitted"]
    provenance_identity: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    content_identity: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    archive_root_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    archive_store: ArchiveStoreName
    encryption_format: str
    passphrase_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    state: Literal[
        "open",
        "closing",
        "uploading",
        "finalizing",
        "finalized",
        "canceled",
        "orphaned",
        "discarding",
    ]
    custody_mode: CollectionUploadCustodyMode
    registration_constraints: CollectionUploadRegistrationConstraintsOut | None
    files_total: int = Field(ge=0, strict=True)
    bytes_total: int = Field(ge=0, strict=True)
    upload_state_expires_at: str | None
    custody: CollectionUploadCustodyOut
    orphaned_at: str | None
    latest_failure: str | None = Field(min_length=1, max_length=1000)
    archive_phase: CollectionUploadArchivePhase
    archive_phase_updated_at: str = Field(pattern=_CANONICAL_UTC_TIMESTAMP_PATTERN)
    archive_next_attempt_at: str | None = Field(pattern=_CANONICAL_UTC_TIMESTAMP_PATTERN)
    archive_storage_prefix: str | None = None
    archive_uploaded_bytes: int | None = None
    archive_total_bytes: int | None = None
    archive_uploaded_units: int | None = None
    archive_total_units: int | None = None
    collection: CollectionSummaryOut | None

    @field_validator("archive_phase_updated_at", "archive_next_attempt_at")
    @classmethod
    def canonical_archive_timestamp(cls, value: str | None) -> str | None:
        return _canonical_timestamp(value) if value is not None else None

    @model_validator(mode="after")
    def validate_terminal_evidence(self) -> CollectionUploadSessionOut:
        if self.state == "finalized" and (
            self.content_identity is None
            or self.archive_root_sha256 is None
            or self.registration_constraints is not None
            or self.collection is None
        ):
            raise ValueError("finalized upload sessions require immutable collection evidence")
        if self.state != "finalized" and (
            self.content_identity is not None
            or self.archive_root_sha256 is not None
            or self.registration_constraints is None
            or self.collection is not None
        ):
            raise ValueError("nonfinal upload sessions require only registration constraints")
        if self.state == "finalized":
            if self.provenance_mode in {"captured", "mixed"}:
                if self.provenance_identity is None:
                    raise ValueError("finalized captured provenance requires its identity")
            elif self.provenance_identity is not None:
                raise ValueError("finalized omitted provenance cannot have an identity")
        elif self.provenance_identity is not None:
            raise ValueError("nonfinal upload sessions cannot have a provenance identity")
        if self.state != "finalized" and self.provenance_mode == "mixed":
            raise ValueError("mixed provenance is only a finalized collection result")
        _validate_upload_custody_state(
            state=self.state,
            custody_mode=self.custody_mode,
            upload_state_expires_at=self.upload_state_expires_at,
            orphaned_at=self.orphaned_at,
        )
        _validate_complete_upload_custody(
            state=self.state,
            custody_mode=self.custody_mode,
            custody_state=self.custody.state,
        )
        _validate_upload_custody_progress(
            files=self.files_total,
            bytes=self.bytes_total,
            custody=self.custody,
        )
        if self.archive_phase == "retry_wait":
            if self.latest_failure is None or self.archive_next_attempt_at is None:
                raise ValueError("retry-wait archive phase requires failure and retry schedule")
        elif self.archive_phase == "finalization_queued":
            if self.latest_failure is not None or self.archive_next_attempt_at is None:
                raise ValueError("queued finalization requires only its eligibility time")
        elif self.archive_next_attempt_at is not None:
            raise ValueError("only queued or retry-wait archive phases may be scheduled")
        if self.archive_phase not in _UPLOAD_ARCHIVE_STATE_PHASES[self.state]:
            raise ValueError("archive phase differs from collection upload state")
        return self


class CreateOrResumeCollectionUploadSessionOut(CollectionUploadSessionOut):
    resumed: bool


class CollectionUploadDiscardPlanOut(RiverhogModel):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"state": {"const": "finalizing"}},
                        "required": ["state"],
                    },
                    "then": {
                        "properties": {
                            "custody": {
                                "properties": {"state": {"const": "complete"}},
                                "required": ["state"],
                            }
                        }
                    },
                }
            ]
        }
    )

    status: Literal["ready", "blocked"]
    collection_id: CollectionId
    warning: str
    expires_at: str
    challenge: str | None
    state: Literal["open", "closing", "uploading", "finalizing", "orphaned", "discarding"]
    files: int = Field(ge=0, strict=True)
    bytes: int = Field(ge=0, strict=True)
    custody: CollectionUploadCustodyOut
    archive_objects: int
    blockers: list[str]

    @model_validator(mode="after")
    def validate_plan(self) -> CollectionUploadDiscardPlanOut:
        if self.status == "ready" and (not self.challenge or self.blockers):
            raise ValueError("ready upload discard plan requires a challenge and no blockers")
        if self.status == "blocked" and (self.challenge is not None or not self.blockers):
            raise ValueError("blocked upload discard plan requires blockers and no challenge")
        if self.status == "ready" and self.state != "orphaned":
            raise ValueError("ready upload discard plan requires orphaned custody")
        _validate_complete_upload_custody(
            state=self.state,
            custody_mode=None,
            custody_state=self.custody.state,
        )
        _validate_upload_custody_progress(
            files=self.files,
            bytes=self.bytes,
            custody=self.custody,
        )
        return self


class DiscardCollectionUploadRequest(RiverhogModel):
    challenge: CanonicalVisibleText


class CollectionUploadDiscardResultOut(RiverhogModel):
    status: Literal["discarded", "already_absent"]
    collection_id: CollectionId
    files: int = Field(ge=0, strict=True)
    bytes: int = Field(ge=0, strict=True)
    custody: CollectionUploadCustodyOut
    archive_objects: int

    @model_validator(mode="after")
    def validate_custody_progress(self) -> CollectionUploadDiscardResultOut:
        _validate_upload_custody_progress(
            files=self.files,
            bytes=self.bytes,
            custody=self.custody,
        )
        return self


CollectionUploadUnitOut = CollectionUploadUnitWorkDocument
CollectionUploadWorkBatchOut = CollectionUploadWorkBatchDocument

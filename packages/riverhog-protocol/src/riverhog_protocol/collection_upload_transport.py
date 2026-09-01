"""Canonical direct collection-upload request documents."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Annotated, Literal, Self

from http_api_contracts import CanonicalVisibleText
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)
from riverhog_provenance_contracts import (
    ProvenanceJournalId,
    ProvenanceJournalStateReference,
    ProvenanceStateId,
)

from riverhog_protocol.collection_workflows import (
    DERIVATION_EVIDENCE_PATH,
    canonical_json_bytes,
    canonical_json_sha256,
)
from riverhog_protocol.file_identity import ImmutableFileIdentityDocument
from riverhog_protocol.paths import (
    CollectionId,
    normalize_relpath,
    validate_collection_id,
)
from riverhog_protocol.raw_ingress import (
    RAW_SOURCE_DIGEST_BATCH_MAX,
    RawSourceDigestSummary,
)
from riverhog_protocol.transport import (
    COLLECTION_UPLOAD_FILE_BATCH_MAX,
    COLLECTION_UPLOAD_UNIT_SOURCE_MAX,
    COLLECTION_UPLOAD_WORK_BATCH_MAX,
)

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
CollectionUploadVolumeId = Annotated[
    str,
    StringConstraints(pattern=r"^(?:pack|segment)-[0-9a-f]{64}$"),
]
CollectionUploadUnitNumber = Annotated[int, Field(ge=0)]
CollectionUploadCustodyMode = Literal["producer-retained", "custody-transfer"]
CollectionUploadVolumeKind = Literal["pack", "segment"]
CollectionUploadUnitState = Literal["pending", "committed"]
CollectionUploadProvenanceJournalState = Literal["accepting", "validating", "sealed", "failed"]


def collection_upload_path_order_key(path: str) -> tuple[int, bytes]:
    """Order v1 payload, Riverhog control evidence, and terminal derivation.

    Transform artifacts may finalize incrementally in arbitrary path ranges.
    Riverhog-owned control evidence follows all payload artifacts, while the
    collection derivation remains the unique terminal member because it binds
    the complete output and generic evidence authorities.
    """

    normalized = normalize_relpath(path)
    rank = 2 if normalized == DERIVATION_EVIDENCE_PATH else int(normalized.startswith("riverhog/"))
    return (rank, normalized.encode("utf-8"))


class CollectionUploadDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CapturedFileProvenanceBinding(ProvenanceJournalStateReference):
    status: Literal["captured"]
    journal_id: ProvenanceJournalId
    current_state_id: ProvenanceStateId


class OmittedFileProvenanceBinding(CollectionUploadDocument):
    status: Literal["omitted"]
    omission_reason: CanonicalVisibleText


FileProvenanceBinding = Annotated[
    CapturedFileProvenanceBinding | OmittedFileProvenanceBinding,
    Field(discriminator="status"),
]


class CollectionUploadRawPartsIn(CollectionUploadDocument):
    part_plaintext_bytes: int = Field(ge=65536)
    part_count: int = Field(ge=1, strict=True)
    ordered_sha256: Sha256


class CollectionUploadRawDigestBatchDocument(CollectionUploadDocument):
    """One append-only bounded slice of a registered raw source digest sequence."""

    path: str
    first_part: int = Field(ge=0, strict=True)
    sha256s: list[Sha256] = Field(
        min_length=1,
        max_length=RAW_SOURCE_DIGEST_BATCH_MAX,
        json_schema_extra={
            "x-riverhog-extent": {
                "policy": "segmented_no_total_max",
                "reason": "bounded-raw-digest-append",
                "progression": "first_part",
            }
        },
    )

    @field_validator("path")
    @classmethod
    def canonical_path(cls, value: str) -> str:
        return normalize_relpath(value)


class CollectionUploadRawDigestProgressDocument(CollectionUploadDocument):
    path: str
    accepted_parts: int = Field(ge=0, strict=True)
    expected_parts: int = Field(ge=1, strict=True)
    complete: bool

    @field_validator("path")
    @classmethod
    def canonical_path(cls, value: str) -> str:
        return normalize_relpath(value)

    @model_validator(mode="after")
    def validate_completion(self) -> Self:
        if self.accepted_parts > self.expected_parts:
            raise ValueError("accepted raw digests exceed the registered part count")
        if self.complete != (self.accepted_parts == self.expected_parts):
            raise ValueError("raw digest completion differs from its exact counts")
        return self


class CollectionUploadProvenanceJournalCreateDocument(CollectionUploadDocument):
    bytes: int = Field(ge=1, strict=True)
    sha256: Sha256


class CollectionUploadProvenanceJournalStatusDocument(CollectionUploadDocument):
    journal_id: ProvenanceJournalId
    state: CollectionUploadProvenanceJournalState
    bytes: int = Field(ge=1, strict=True)
    sha256: Sha256
    accepted_bytes: int = Field(ge=0, strict=True)
    failure: str | None = None
    current_state_id: ProvenanceStateId | None = None
    current_path: str | None = None
    current_bytes: int | None = Field(default=None, ge=0, strict=True)
    current_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def validate_progress(self) -> Self:
        if self.accepted_bytes > self.bytes:
            raise ValueError("accepted provenance bytes exceed the declared authority")
        if self.state in {"validating", "sealed"} and self.accepted_bytes != self.bytes:
            raise ValueError("closed provenance content is incomplete")
        summary = (
            self.current_state_id,
            self.current_path,
            self.current_bytes,
            self.current_sha256,
        )
        if self.state == "sealed":
            if any(value is None for value in summary) or self.failure is not None:
                raise ValueError("sealed provenance journal requires its current state")
        elif any(value is not None for value in summary):
            raise ValueError("unsealed provenance journal cannot expose a current state")
        if (self.state == "failed") != (self.failure is not None):
            raise ValueError("provenance journal failure differs from its state")
        return self


class CollectionUploadRegistrationConstraintsDocument(CollectionUploadDocument):
    """Producer constraints issued by Riverhog for one upload session."""

    pack_member_bytes: int = Field(ge=1)
    raw_part_plaintext_bytes: int = Field(ge=65536, multiple_of=65536)


class CollectionUploadUnitSourceDocument(CollectionUploadDocument):
    """One exact source range supplied in a server-planned upload unit."""

    path: str
    offset: int = Field(ge=0, strict=True)
    bytes: int = Field(ge=0, strict=True)
    artifact_sha256: Sha256

    @field_validator("path")
    @classmethod
    def canonical_path(cls, value: str) -> str:
        return normalize_relpath(value)


class CollectionUploadUnitDocument(CollectionUploadDocument):
    """Protocol-owned identity of one server-planned plaintext upload unit."""

    unit: CollectionUploadUnitNumber
    payload_bytes: int = Field(ge=0, strict=True)
    plaintext_bytes: int = Field(ge=0, strict=True)
    sources: list[CollectionUploadUnitSourceDocument] = Field(
        max_length=COLLECTION_UPLOAD_UNIT_SOURCE_MAX,
        json_schema_extra={
            "x-riverhog-extent": {
                "policy": "segmented_no_total_max",
                "reason": "bounded-upload-unit-source-map",
                "progression": "collection-volume-sequence",
            }
        },
    )

    @model_validator(mode="after")
    def validate_sources(self) -> Self:
        if sum(source.bytes for source in self.sources) != self.payload_bytes:
            raise ValueError("upload unit source bytes differ from its payload bytes")
        identities = [(source.path, source.offset) for source in self.sources]
        if len(identities) != len(set(identities)):
            raise ValueError("upload unit source ranges must be unique")
        return self


class CollectionUploadVolumeSummaryDocument(CollectionUploadDocument):
    """Protocol-owned identity of one immutable collection archive volume."""

    volume_id: CollectionUploadVolumeId
    sequence: int = Field(ge=0, strict=True)
    kind: CollectionUploadVolumeKind

    @model_validator(mode="after")
    def validate_volume_identity(self) -> Self:
        expected_prefix = "pack" if self.kind == "pack" else "segment"
        if self.sequence >= 1 << 256:
            raise ValueError("upload volume sequence exceeds its v1 representation")
        if self.volume_id != f"{expected_prefix}-{self.sequence:064x}":
            raise ValueError("upload volume ID differs from its kind and sequence")
        return self


class CollectionUploadUnitWorkDocument(CollectionUploadUnitDocument):
    """One exact unit and its durable upload checkpoint state."""

    state: CollectionUploadUnitState


class CollectionUploadUnitAssignmentDocument(CollectionUploadDocument):
    """One bounded, immutable unit offered by an exact upload session."""

    volume: CollectionUploadVolumeSummaryDocument
    plan_sha256: Sha256
    unit: CollectionUploadUnitWorkDocument


class CollectionUploadWorkBatchDocument(CollectionUploadDocument):
    """A bounded acquisition step over currently actionable upload units."""

    collection_id: CollectionId
    planning_complete: bool
    complete: bool
    committed_payload_bytes: int = Field(ge=0, strict=True)
    work: list[CollectionUploadUnitAssignmentDocument] = Field(
        max_length=COLLECTION_UPLOAD_WORK_BATCH_MAX,
        json_schema_extra={
            "x-riverhog-extent": {
                "policy": "segmented_no_total_max",
                "reason": "bounded-actionable-work-acquisition",
                "progression": "repeated-acquisition-until-complete",
            }
        },
    )

    @field_validator("collection_id")
    @classmethod
    def canonical_collection_id(cls, value: int) -> int:
        return validate_collection_id(value)

    @model_validator(mode="after")
    def validate_completion(self) -> Self:
        if self.complete != (self.planning_complete and not self.work):
            raise ValueError("upload work completion differs from planning and actionable work")
        identities = [(item.volume.volume_id, item.unit.unit) for item in self.work]
        if len(identities) != len(set(identities)):
            raise ValueError("upload work assignments must be unique")
        return self


class CollectionUploadFileIn(ImmutableFileIdentityDocument):
    raw_parts: CollectionUploadRawPartsIn | None = None
    provenance: FileProvenanceBinding | None = None


class CollectionUploadFileBatchDocument(CollectionUploadDocument):
    files: list[CollectionUploadFileIn] = Field(
        min_length=1,
        max_length=COLLECTION_UPLOAD_FILE_BATCH_MAX,
        json_schema_extra={
            "x-riverhog-extent": {
                "policy": "segmented_no_total_max",
                "reason": "bounded-upload-registration",
                "progression": "repeated-canonical-registration",
            }
        },
    )

    @model_validator(mode="after")
    def validate_canonical_file_order(self) -> Self:
        if self.files != sorted(
            self.files,
            key=lambda item: collection_upload_path_order_key(item.path),
        ):
            raise ValueError("collection upload files must be in canonical path order")
        paths = [item.path for item in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("collection upload file paths must be unique")
        return self


class CollectionUploadCustodyObjectDocument(CollectionUploadDocument):
    volume_id: CollectionUploadVolumeId
    sealed_receipt_sha256: Sha256


class CollectionUploadArtifactCustodyReceiptDocument(CollectionUploadDocument):
    """Exact safe-release evidence for one artifact in construction state."""

    format: Literal["riverhog-artifact-custody-receipt/v1"] = "riverhog-artifact-custody-receipt/v1"
    collection_id: CollectionId
    path: str
    bytes: int = Field(ge=0, strict=True)
    sha256: Sha256
    archive_object_count: int = Field(ge=1, strict=True)
    archive_object_set_sha256: Sha256
    receipt_sha256: Sha256

    @field_validator("collection_id")
    @classmethod
    def canonical_collection_id(cls, value: int) -> int:
        return validate_collection_id(value)

    @field_validator("path")
    @classmethod
    def canonical_path(cls, value: str) -> str:
        return normalize_relpath(value)

    @model_validator(mode="after")
    def validate_receipt(self) -> Self:
        payload = self.model_dump(mode="json", exclude={"receipt_sha256"})
        if canonical_json_sha256(payload) != self.receipt_sha256:
            raise ValueError("artifact custody receipt identity differs from its payload")
        return self

    @classmethod
    def seal(
        cls,
        *,
        collection_id: int,
        path: str,
        bytes: int,
        sha256: str,
        archive_objects: Sequence[CollectionUploadCustodyObjectDocument],
    ) -> CollectionUploadArtifactCustodyReceiptDocument:
        digest = hashlib.sha256()
        count = 0
        previous: str | None = None
        for item in archive_objects:
            if previous is not None and item.volume_id <= previous:
                raise ValueError("custody receipt archive objects must be unique and ordered")
            encoded = canonical_json_bytes(item.model_dump(mode="json"))
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            previous = item.volume_id
            count += 1
        if count < 1:
            raise ValueError("custody receipt requires at least one archive object")
        payload = {
            "format": "riverhog-artifact-custody-receipt/v1",
            "collection_id": collection_id,
            "path": path,
            "bytes": bytes,
            "sha256": sha256,
            "archive_object_count": count,
            "archive_object_set_sha256": digest.hexdigest(),
        }
        return cls(
            format="riverhog-artifact-custody-receipt/v1",
            collection_id=collection_id,
            path=path,
            bytes=bytes,
            sha256=sha256,
            archive_object_count=count,
            archive_object_set_sha256=digest.hexdigest(),
            receipt_sha256=canonical_json_sha256(payload),
        )


def validate_collection_upload_artifact_custody_receipt(
    collection_id: int,
    artifact: ImmutableFileIdentityDocument,
    receipt: CollectionUploadArtifactCustodyReceiptDocument,
) -> CollectionUploadArtifactCustodyReceiptDocument:
    """Bind one safe-release receipt to its exact session artifact."""

    expected_collection_id = validate_collection_id(collection_id)
    if (
        receipt.collection_id != expected_collection_id
        or receipt.path != artifact.path
        or receipt.bytes != artifact.bytes
        or receipt.sha256 != artifact.sha256
    ):
        raise ValueError("artifact custody receipt differs from its upload file identity")
    return receipt


def validate_collection_upload_batch_against_registration_constraints(
    batch: CollectionUploadFileBatchDocument,
    constraints: CollectionUploadRegistrationConstraintsDocument,
) -> CollectionUploadFileBatchDocument:
    """Validate registration identities against server-issued constraints."""

    for item in batch.files:
        collection_upload_raw_digest_summary(item, constraints)
    return batch


def collection_upload_raw_digest_summary(
    item: CollectionUploadFileIn,
    constraints: CollectionUploadRegistrationConstraintsDocument,
) -> RawSourceDigestSummary | None:
    """Return the bounded raw digest authority required for one large file."""

    raw = item.raw_parts
    if item.bytes < constraints.pack_member_bytes:
        if raw is not None:
            raise ValueError(f"raw part digests are only valid for large file: {item.path}")
        return None
    if raw is None:
        raise ValueError(f"raw part digests are required for large file: {item.path}")
    if raw.part_plaintext_bytes != constraints.raw_part_plaintext_bytes:
        raise ValueError(f"raw part digest policy does not match the session: {item.path}")
    return RawSourceDigestSummary(
        path=item.path,
        bytes=item.bytes,
        sha256=item.sha256,
        part_plaintext_bytes=raw.part_plaintext_bytes,
        part_count=raw.part_count,
        ordered_part_sha256=raw.ordered_sha256,
    )


__all__ = [
    "CapturedFileProvenanceBinding",
    "CollectionUploadFileBatchDocument",
    "CollectionUploadFileIn",
    "CollectionUploadArtifactCustodyReceiptDocument",
    "CollectionUploadCustodyMode",
    "CollectionUploadCustodyObjectDocument",
    "CollectionUploadRegistrationConstraintsDocument",
    "CollectionUploadRawDigestBatchDocument",
    "CollectionUploadRawDigestProgressDocument",
    "CollectionUploadRawPartsIn",
    "CollectionUploadProvenanceJournalCreateDocument",
    "CollectionUploadProvenanceJournalState",
    "CollectionUploadProvenanceJournalStatusDocument",
    "CollectionUploadUnitDocument",
    "CollectionUploadUnitAssignmentDocument",
    "CollectionUploadUnitNumber",
    "CollectionUploadUnitSourceDocument",
    "CollectionUploadUnitState",
    "CollectionUploadUnitWorkDocument",
    "CollectionUploadWorkBatchDocument",
    "CollectionUploadVolumeId",
    "CollectionUploadVolumeKind",
    "CollectionUploadVolumeSummaryDocument",
    "FileProvenanceBinding",
    "OmittedFileProvenanceBinding",
    "collection_upload_raw_digest_summary",
    "collection_upload_path_order_key",
    "validate_collection_upload_artifact_custody_receipt",
    "validate_collection_upload_batch_against_registration_constraints",
]

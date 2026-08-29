"""Canonical direct collection-upload request documents."""

from __future__ import annotations

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
    canonical_json_sha256,
)
from riverhog_protocol.file_identity import ImmutableFileIdentityDocument
from riverhog_protocol.paths import (
    CollectionId,
    normalize_relpath,
    validate_collection_id,
)
from riverhog_protocol.raw_ingress import RawSourceDigestManifest
from riverhog_protocol.transport import COLLECTION_UPLOAD_FILE_BATCH_MAX

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
CollectionUploadVolumeId = Annotated[
    str,
    StringConstraints(pattern=r"^(?:pack|segment)-[0-9]{12}$"),
]
CollectionUploadUnitNumber = Annotated[int, Field(ge=0)]
CollectionUploadCustodyMode = Literal["producer-retained", "custody-transfer"]
CollectionUploadVolumeKind = Literal["pack", "segment"]
CollectionUploadVolumeState = Literal["planned", "uploading", "sealed"]
CollectionUploadUnitState = Literal["pending", "committed"]


def collection_upload_path_order_key(path: str) -> tuple[int, bytes]:
    """Order v1 upload members while keeping terminal derivation evidence last.

    A collection derivation binds the complete output set and therefore cannot
    exist while a transform is still producing artifacts. All other member
    paths retain their ordinary UTF-8 lexical order.
    """

    normalized = normalize_relpath(path)
    return (1 if normalized == DERIVATION_EVIDENCE_PATH else 0, normalized.encode("utf-8"))


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
    sha256s: list[Sha256] = Field(min_length=1)


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
    sources: Sequence[CollectionUploadUnitSourceDocument]

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
        if self.volume_id != f"{expected_prefix}-{self.sequence:012d}":
            raise ValueError("upload volume ID differs from its kind and sequence")
        return self


class CollectionUploadVolumeDocument(CollectionUploadVolumeSummaryDocument):
    """Protocol-owned identity and complete unit plan for one archive volume."""

    plan_sha256: Sha256
    plaintext_bytes: int = Field(ge=0, strict=True)
    source_bytes: int = Field(ge=0, strict=True)
    units: Sequence[CollectionUploadUnitDocument] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_volume_plan(self) -> Self:
        if [unit.unit for unit in self.units] != list(range(len(self.units))):
            raise ValueError("upload volume units must be consecutive from zero")
        if sum(unit.plaintext_bytes for unit in self.units) != self.plaintext_bytes:
            raise ValueError("upload volume unit plaintext bytes differ from its total")
        if sum(unit.payload_bytes for unit in self.units) != self.source_bytes:
            raise ValueError("upload volume unit payload bytes differ from its source total")
        return self


class CollectionUploadUnitWorkDocument(CollectionUploadUnitDocument):
    """One exact unit and its durable upload checkpoint state."""

    state: CollectionUploadUnitState


class CollectionUploadVolumeWorkDocument(CollectionUploadVolumeDocument):
    """One exact volume plan and its durable construction state."""

    state: CollectionUploadVolumeState
    units: Sequence[CollectionUploadUnitWorkDocument] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_work_state(self) -> Self:
        committed = sum(unit.state == "committed" for unit in self.units)
        if self.state == "planned" and committed != 0:
            raise ValueError("planned upload volumes cannot contain committed units")
        if self.state == "uploading" and committed == len(self.units):
            raise ValueError("uploading volumes must contain a pending unit")
        if self.state == "sealed" and committed != len(self.units):
            raise ValueError("upload volume state differs from its unit checkpoints")
        return self


class CollectionUploadVolumeSetDocument(CollectionUploadDocument):
    """Complete canonically ordered upload work for one collection session."""

    collection_id: CollectionId
    volumes: Sequence[CollectionUploadVolumeWorkDocument]

    @field_validator("collection_id")
    @classmethod
    def canonical_collection_id(cls, value: int) -> int:
        return validate_collection_id(value)

    @model_validator(mode="after")
    def validate_complete_volume_set(self) -> Self:
        sequences = [item.sequence for item in self.volumes]
        identities = [item.volume_id for item in self.volumes]
        if sequences != list(range(len(self.volumes))):
            raise ValueError("upload volumes must be consecutive from sequence zero")
        if len(identities) != len(set(identities)):
            raise ValueError("upload volume IDs must be unique")
        return self


class CollectionUploadFileIn(ImmutableFileIdentityDocument):
    raw_parts: CollectionUploadRawPartsIn | None = None
    provenance: FileProvenanceBinding


class CollectionUploadFileBatchDocument(CollectionUploadDocument):
    files: list[CollectionUploadFileIn] = Field(
        min_length=1,
        max_length=COLLECTION_UPLOAD_FILE_BATCH_MAX,
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
    archive_objects: tuple[CollectionUploadCustodyObjectDocument, ...] = Field(min_length=1)
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
        object_ids = [item.volume_id for item in self.archive_objects]
        if object_ids != sorted(set(object_ids)):
            raise ValueError("custody receipt archive objects must be unique and ordered")
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
        archive_objects: tuple[CollectionUploadCustodyObjectDocument, ...],
    ) -> CollectionUploadArtifactCustodyReceiptDocument:
        payload = {
            "format": "riverhog-artifact-custody-receipt/v1",
            "collection_id": collection_id,
            "path": path,
            "bytes": bytes,
            "sha256": sha256,
            "archive_objects": [item.model_dump(mode="json") for item in archive_objects],
        }
        return cls(
            format="riverhog-artifact-custody-receipt/v1",
            collection_id=collection_id,
            path=path,
            bytes=bytes,
            sha256=sha256,
            archive_objects=archive_objects,
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
        collection_upload_raw_digest_manifest(item, constraints)
    return batch


def collection_upload_raw_digest_manifest(
    item: CollectionUploadFileIn,
    constraints: CollectionUploadRegistrationConstraintsDocument,
) -> RawSourceDigestManifest | None:
    """Return the raw manifest required by the session constraints for one file."""

    raw = item.raw_parts
    if item.bytes < constraints.pack_member_bytes:
        if raw is not None:
            raise ValueError(f"raw part digests are only valid for large file: {item.path}")
        return None
    if raw is None:
        raise ValueError(f"raw part digests are required for large file: {item.path}")
    if raw.part_plaintext_bytes != constraints.raw_part_plaintext_bytes:
        raise ValueError(f"raw part digest policy does not match the session: {item.path}")
    return RawSourceDigestManifest(
        path=item.path,
        bytes=item.bytes,
        sha256=item.sha256,
        part_plaintext_bytes=raw.part_plaintext_bytes,
        part_sha256s=tuple(raw.sha256s),
    )


__all__ = [
    "CapturedFileProvenanceBinding",
    "CollectionUploadFileBatchDocument",
    "CollectionUploadFileIn",
    "CollectionUploadArtifactCustodyReceiptDocument",
    "CollectionUploadCustodyMode",
    "CollectionUploadCustodyObjectDocument",
    "CollectionUploadRegistrationConstraintsDocument",
    "CollectionUploadRawPartsIn",
    "CollectionUploadUnitDocument",
    "CollectionUploadUnitNumber",
    "CollectionUploadUnitSourceDocument",
    "CollectionUploadUnitState",
    "CollectionUploadUnitWorkDocument",
    "CollectionUploadVolumeDocument",
    "CollectionUploadVolumeId",
    "CollectionUploadVolumeKind",
    "CollectionUploadVolumeSetDocument",
    "CollectionUploadVolumeState",
    "CollectionUploadVolumeSummaryDocument",
    "CollectionUploadVolumeWorkDocument",
    "FileProvenanceBinding",
    "OmittedFileProvenanceBinding",
    "collection_upload_raw_digest_manifest",
    "collection_upload_path_order_key",
    "validate_collection_upload_artifact_custody_receipt",
    "validate_collection_upload_batch_against_registration_constraints",
]

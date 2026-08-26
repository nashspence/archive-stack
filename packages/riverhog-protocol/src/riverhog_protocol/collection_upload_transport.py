"""Canonical direct collection-upload request documents."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from http_api_contracts import CanonicalVisibleText
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator
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
    CanonicalTag,
    CollectionId,
    normalize_relpath,
    validate_collection_id,
)
from riverhog_protocol.raw_ingress import RawSourceDigestManifest
from riverhog_protocol.storage_names import ArchiveStoreName
from riverhog_protocol.transport import COLLECTION_UPLOAD_FILE_BATCH_MAX

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
CollectionUploadCustodyMode = Literal["producer-retained", "custody-transfer"]


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


class CollectionUploadCreationIdentityPayload(CollectionUploadDocument):
    """Normalized create-or-resume identity retained after construction."""

    format: Literal["riverhog-collection-upload-creation/v1"] = (
        "riverhog-collection-upload-creation/v1"
    )
    tags: tuple[CanonicalTag, ...]
    ingest_source: str | None = None
    archive_store: ArchiveStoreName
    event_context: dict[str, JsonValue] | None = None
    provenance_mode: Literal["captured", "omitted"]
    provenance_omission_reason: CanonicalVisibleText | None = None
    custody_mode: CollectionUploadCustodyMode

    @field_validator("tags")
    @classmethod
    def canonical_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(value)) or len(value) != len(set(value)):
            raise ValueError("collection upload creation tags must be unique and ordered")
        return value

    @model_validator(mode="after")
    def validate_provenance_choice(self) -> Self:
        if self.provenance_mode == "captured":
            if self.provenance_omission_reason is not None:
                raise ValueError("captured provenance cannot have an omission reason")
        elif self.provenance_omission_reason is None:
            raise ValueError("omitted provenance requires an omission reason")
        return self


class CollectionUploadCreationIdentityDocument(CollectionUploadCreationIdentityPayload):
    creation_identity_sha256: Sha256

    @model_validator(mode="after")
    def verify_identity(self) -> Self:
        payload = self.model_dump(
            mode="json",
            exclude={"creation_identity_sha256"},
            exclude_none=True,
        )
        if canonical_json_sha256(payload) != self.creation_identity_sha256:
            raise ValueError("collection upload creation identity differs from its payload")
        return self

    @classmethod
    def seal(
        cls,
        payload: CollectionUploadCreationIdentityPayload,
    ) -> CollectionUploadCreationIdentityDocument:
        document = payload.model_dump(mode="python", exclude_none=True)
        return cls(
            **document,
            creation_identity_sha256=canonical_json_sha256(document),
        )


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
    volume_id: CanonicalVisibleText
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
    "CollectionUploadCreationIdentityDocument",
    "CollectionUploadCreationIdentityPayload",
    "CollectionUploadRegistrationConstraintsDocument",
    "CollectionUploadRawPartsIn",
    "FileProvenanceBinding",
    "OmittedFileProvenanceBinding",
    "collection_upload_raw_digest_manifest",
    "collection_upload_path_order_key",
    "validate_collection_upload_artifact_custody_receipt",
    "validate_collection_upload_batch_against_registration_constraints",
]

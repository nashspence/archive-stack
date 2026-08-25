"""Canonical direct collection-upload request documents."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from http_api_contracts import CanonicalVisibleText
from pydantic import BaseModel, ConfigDict, Field, model_validator
from riverhog_provenance_contracts import (
    ProvenanceJournalId,
    ProvenanceJournalStateReference,
    ProvenanceStateId,
)

from riverhog_protocol.paths import CanonicalRelPath
from riverhog_protocol.raw_ingress import RawSourceDigestManifest
from riverhog_protocol.transport import COLLECTION_UPLOAD_FILE_BATCH_MAX

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


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


class CollectionUploadFileIn(CollectionUploadDocument):
    path: CanonicalRelPath
    bytes: int = Field(ge=0)
    sha256: Sha256
    raw_parts: CollectionUploadRawPartsIn | None = None
    provenance: FileProvenanceBinding


class CollectionUploadFileBatchDocument(CollectionUploadDocument):
    files: list[CollectionUploadFileIn] = Field(
        min_length=1,
        max_length=COLLECTION_UPLOAD_FILE_BATCH_MAX,
    )

    @model_validator(mode="after")
    def validate_canonical_file_order(self) -> Self:
        if self.files != sorted(self.files, key=lambda item: item.path):
            raise ValueError("collection upload files must be in canonical path order")
        paths = [item.path for item in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("collection upload file paths must be unique")
        return self


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
    "CollectionUploadRegistrationConstraintsDocument",
    "CollectionUploadRawPartsIn",
    "FileProvenanceBinding",
    "OmittedFileProvenanceBinding",
    "collection_upload_raw_digest_manifest",
    "validate_collection_upload_batch_against_registration_constraints",
]

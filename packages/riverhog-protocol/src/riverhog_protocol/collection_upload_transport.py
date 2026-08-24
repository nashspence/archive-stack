"""Canonical direct collection-upload request documents."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from http_api_contracts import CanonicalVisibleText
from pydantic import BaseModel, ConfigDict, Field, model_validator

from riverhog_protocol.paths import CanonicalRelPath
from riverhog_protocol.transport import COLLECTION_UPLOAD_FILE_BATCH_MAX

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class CollectionUploadDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CapturedFileProvenanceBinding(CollectionUploadDocument):
    status: Literal["captured"]
    journal_id: str = Field(min_length=1)
    current_state_id: str = Field(min_length=1)


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
        return self


__all__ = [
    "CapturedFileProvenanceBinding",
    "CollectionUploadFileBatchDocument",
    "CollectionUploadFileIn",
    "CollectionUploadRawPartsIn",
    "FileProvenanceBinding",
    "OmittedFileProvenanceBinding",
]

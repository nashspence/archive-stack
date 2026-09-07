"""Server-owned identity for create-or-resume collection construction state."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from http_api_contracts import CanonicalVisibleText
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator
from riverhog_protocol import CollectionDescription, CollectionUploadCustodyMode
from riverhog_protocol.collection_workflows import canonical_json_sha256
from riverhog_protocol.storage_names import ArchiveStoreName

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class CollectionUploadCreationIdentityPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    format: Literal["riverhog-collection-upload-creation/v1"] = (
        "riverhog-collection-upload-creation/v1"
    )
    ingest_source: str | None = None
    description: CollectionDescription | None = None
    archive_store: ArchiveStoreName
    event_context: dict[str, JsonValue] | None = None
    provenance_mode: Literal["captured", "omitted"]
    provenance_omission_reason: CanonicalVisibleText | None = None
    custody_mode: CollectionUploadCustodyMode

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


__all__ = [
    "CollectionUploadCreationIdentityDocument",
    "CollectionUploadCreationIdentityPayload",
]

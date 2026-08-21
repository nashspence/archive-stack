"""Published provider-neutral object-storage contract for Riverhog."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from riverhog_storage_adapter_protocol.jcs import canonical_json_sha256

STORAGE_ADAPTER_PROTOCOL: Literal["riverhog-storage-adapter/v1"] = "riverhog-storage-adapter/v1"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
SEMANTIC_ID_PATTERN = r"^[a-z0-9](?:[a-z0-9._/-]{0,158}[a-z0-9])?$"
TRANSFER_ID_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9._:-]{0,198}[A-Za-z0-9])?$"

Sha256 = Annotated[str, StringConstraints(pattern=SHA256_PATTERN)]
SemanticId = Annotated[str, StringConstraints(pattern=SEMANTIC_ID_PATTERN)]
TransferId = Annotated[str, StringConstraints(pattern=TRANSFER_ID_PATTERN)]
ReadMode = Literal["immediate", "restore_required"]
UploadState = Literal["open", "completed", "aborted"]
ReadState = Literal["ready", "requested", "expired"]
StorageAdapterErrorCode = Literal[
    "unauthorized",
    "not_found",
    "revision_conflict",
    "upload_conflict",
    "invalid_path",
    "invalid_range",
    "read_not_ready",
    "read_expired",
    "integrity_failure",
    "provider_unavailable",
    "internal_failure",
]


def normalize_object_path(value: str, *, allow_prefix: bool = False) -> str:
    """Return one canonical relative POSIX object path."""

    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or "\x00" in value
        or value.endswith("/")
    ):
        raise ValueError("object path must be a nonempty relative POSIX path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("object path contains a forbidden segment")
    normalized = "/".join(parts)
    if not allow_prefix and normalized != value:
        raise ValueError("object path must be canonical")
    return normalized


def _without_digest(model: BaseModel, field: str) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude={field}, exclude_none=True)


class StorageAdapterModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StorageProfilePayload(StorageAdapterModel):
    protocol: Literal["riverhog-storage-adapter/v1"] = STORAGE_ADAPTER_PROTOCOL
    profile_id: SemanticId
    read_mode: ReadMode
    egress_accounting_id: SemanticId


class StorageProfile(StorageProfilePayload):
    profile_contract_sha256: Sha256

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        expected = canonical_json_sha256(_without_digest(self, "profile_contract_sha256"))
        if expected != self.profile_contract_sha256:
            raise ValueError("storage profile digest does not match its canonical payload")
        return self

    @classmethod
    def seal(cls, payload: StorageProfilePayload) -> StorageProfile:
        document = payload.model_dump(mode="json", exclude_none=True)
        return cls(
            **document,
            profile_contract_sha256=canonical_json_sha256(document),
        )


class StorageAdapterDescriptorPayload(StorageAdapterModel):
    protocol: Literal["riverhog-storage-adapter/v1"] = STORAGE_ADAPTER_PROTOCOL
    implementation_id: SemanticId
    implementation_version: str = Field(min_length=1, max_length=120)
    source_revision: str = Field(min_length=1, max_length=200)
    profile: StorageProfile
    minimum_nonfinal_part_bytes: int = Field(ge=1)
    maximum_part_bytes: int = Field(ge=1)
    maximum_part_count: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_multipart_limits(self) -> Self:
        if self.minimum_nonfinal_part_bytes > self.maximum_part_bytes:
            raise ValueError("minimum non-final part bytes must not exceed maximum part bytes")
        return self


class StorageAdapterDescriptor(StorageAdapterDescriptorPayload):
    runtime_descriptor_sha256: Sha256

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        expected = canonical_json_sha256(_without_digest(self, "runtime_descriptor_sha256"))
        if expected != self.runtime_descriptor_sha256:
            raise ValueError("runtime descriptor digest does not match its canonical payload")
        return self

    @classmethod
    def seal(cls, payload: StorageAdapterDescriptorPayload) -> StorageAdapterDescriptor:
        document = payload.model_dump(mode="json", exclude_none=True)
        return cls(
            **document,
            runtime_descriptor_sha256=canonical_json_sha256(document),
        )


class WriteCondition(StorageAdapterModel):
    mode: Literal["create_only", "replace_exact"] = "create_only"
    prior_revision: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_revision(self) -> Self:
        if self.mode == "create_only" and self.prior_revision is not None:
            raise ValueError("create-only writes must not name a prior revision")
        if self.mode == "replace_exact" and self.prior_revision is None:
            raise ValueError("exact replacement requires a prior revision")
        return self


class UploadDeclarationPayload(StorageAdapterModel):
    protocol: Literal["riverhog-storage-adapter/v1"] = STORAGE_ADAPTER_PROTOCOL
    transfer_id: TransferId
    object_path: str = Field(min_length=1, max_length=4096)
    content_type: str = Field(min_length=1, max_length=255)
    stored_bytes: int = Field(ge=0)
    runtime_descriptor_sha256: Sha256
    condition: WriteCondition = Field(default_factory=WriteCondition)

    @field_validator("object_path")
    @classmethod
    def canonical_object_path(cls, value: str) -> str:
        return normalize_object_path(value)


class UploadDeclaration(UploadDeclarationPayload):
    request_sha256: Sha256

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        expected = canonical_json_sha256(_without_digest(self, "request_sha256"))
        if expected != self.request_sha256:
            raise ValueError("upload request digest does not match its canonical payload")
        return self

    @classmethod
    def seal(cls, payload: UploadDeclarationPayload) -> UploadDeclaration:
        document = payload.model_dump(mode="json", exclude_none=True)
        return cls(**document, request_sha256=canonical_json_sha256(document))


class UploadPartReceipt(StorageAdapterModel):
    number: int = Field(ge=1)
    part_token: str = Field(min_length=1, max_length=2000)
    stored_bytes: int = Field(ge=1)
    stored_sha256: Sha256


class ObjectReceipt(StorageAdapterModel):
    object_path: str = Field(min_length=1, max_length=4096)
    revision: str = Field(min_length=1, max_length=1000)
    content_type: str = Field(min_length=1, max_length=255)
    stored_bytes: int = Field(ge=0)
    stored_sha256: Sha256
    completed_at: str = Field(min_length=1, max_length=100)

    @field_validator("object_path")
    @classmethod
    def canonical_object_path(cls, value: str) -> str:
        return normalize_object_path(value)


class CompleteUploadRequest(StorageAdapterModel):
    parts: tuple[UploadPartReceipt, ...]
    stored_bytes: int = Field(ge=0)
    stored_sha256: Sha256

    @field_validator("parts")
    @classmethod
    def canonical_parts(cls, value: tuple[UploadPartReceipt, ...]) -> tuple[UploadPartReceipt, ...]:
        if [part.number for part in value] != list(range(1, len(value) + 1)):
            raise ValueError("upload parts must be contiguous and ordered from one")
        if len({part.part_token for part in value}) != len(value):
            raise ValueError("upload part tokens must be unique")
        return value

    @model_validator(mode="after")
    def validate_size(self) -> Self:
        if self.stored_bytes == 0 and self.parts:
            raise ValueError("zero-byte completion must not contain parts")
        if self.stored_bytes > 0 and not self.parts:
            raise ValueError("nonempty completion requires parts")
        if sum(part.stored_bytes for part in self.parts) != self.stored_bytes:
            raise ValueError("completed byte count must equal the ordered part bytes")
        return self


class UploadStatus(StorageAdapterModel):
    declaration: UploadDeclaration
    state: UploadState
    parts: tuple[UploadPartReceipt, ...] = ()
    object: ObjectReceipt | None = None

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.state == "completed" and self.object is None:
            raise ValueError("completed upload status requires an object receipt")
        if self.state != "completed" and self.object is not None:
            raise ValueError("only completed uploads may contain an object receipt")
        return self


class ObjectLocator(StorageAdapterModel):
    object_path: str = Field(min_length=1, max_length=4096)
    revision: str = Field(min_length=1, max_length=1000)

    @field_validator("object_path")
    @classmethod
    def canonical_object_path(cls, value: str) -> str:
        return normalize_object_path(value)


class ObjectDeleteRequest(StorageAdapterModel):
    object: ObjectLocator


class PrefixDeleteRequest(StorageAdapterModel):
    object_prefix: str = Field(min_length=1, max_length=4096)

    @field_validator("object_prefix")
    @classmethod
    def canonical_prefix(cls, value: str) -> str:
        return normalize_object_path(value, allow_prefix=True)


class ReadRequest(StorageAdapterModel):
    objects: tuple[ObjectLocator, ...] = Field(min_length=1)

    @field_validator("objects")
    @classmethod
    def canonical_objects(cls, value: tuple[ObjectLocator, ...]) -> tuple[ObjectLocator, ...]:
        identities = [(item.object_path, item.revision) for item in value]
        if identities != sorted(identities) or len(identities) != len(set(identities)):
            raise ValueError("read objects must be unique and ordered by path and revision")
        return value


class ReadStatus(StorageAdapterModel):
    state: ReadState
    ready_at: str | None = Field(default=None, max_length=100)
    expires_at: str | None = Field(default=None, max_length=100)
    message: str | None = Field(default=None, max_length=1000)


class AbortIncompleteUploadsRequest(StorageAdapterModel):
    initiated_before: str = Field(min_length=1, max_length=100)


class MaintenanceResult(StorageAdapterModel):
    affected: int = Field(ge=0)


class StorageAdapterError(StorageAdapterModel):
    code: StorageAdapterErrorCode
    message: str = Field(min_length=1, max_length=2000)


def require_sha256(value: str, *, name: str) -> str:
    if re.fullmatch(SHA256_PATTERN, value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


__all__ = [
    "AbortIncompleteUploadsRequest",
    "CompleteUploadRequest",
    "MaintenanceResult",
    "ObjectDeleteRequest",
    "ObjectLocator",
    "ObjectReceipt",
    "PrefixDeleteRequest",
    "ReadMode",
    "ReadRequest",
    "ReadState",
    "ReadStatus",
    "SEMANTIC_ID_PATTERN",
    "SHA256_PATTERN",
    "STORAGE_ADAPTER_PROTOCOL",
    "SemanticId",
    "Sha256",
    "StorageAdapterDescriptor",
    "StorageAdapterDescriptorPayload",
    "StorageAdapterError",
    "StorageAdapterErrorCode",
    "StorageAdapterModel",
    "StorageProfile",
    "StorageProfilePayload",
    "TransferId",
    "UploadDeclaration",
    "UploadDeclarationPayload",
    "UploadPartReceipt",
    "UploadState",
    "UploadStatus",
    "WriteCondition",
    "normalize_object_path",
    "require_sha256",
]

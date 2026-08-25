"""Provider-neutral object capabilities carried across the adapter boundary.

The models mirror Riverhog's existing object-store ports. They deliberately do
not define a transfer job, journal, archive object, collection, or provider
configuration model.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Annotated, Literal, Protocol, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

STORAGE_ADAPTER_PROTOCOL: Literal["riverhog-storage-adapter/v1"] = "riverhog-storage-adapter/v1"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SEMANTIC_ID_PATTERN = r"^[a-z0-9](?:[a-z0-9._/-]{0,158}[a-z0-9])?$"
_METADATA_KEY_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
_MAX_IDENTITY_METADATA_ITEMS = 64
_MAX_IDENTITY_METADATA_BYTES = 16 * 1024

Sha256 = Annotated[str, StringConstraints(pattern=_SHA256_PATTERN)]
SemanticId = Annotated[str, StringConstraints(pattern=_SEMANTIC_ID_PATTERN)]
ObjectPlacement = Literal["archive", "immediate"]
ReadMode = Literal["immediate", "restore_required"]
ReadState = Literal["ready", "requested", "expired"]
StorageAdapterErrorCode = Literal[
    "unauthorized",
    "invalid_request",
    "not_found",
    "method_not_allowed",
    "request_too_large",
    "identity_conflict",
    "invalid_path",
    "invalid_range",
    "read_not_ready",
    "read_expired",
    "integrity_failure",
    "provider_unavailable",
    "internal_failure",
]
OpaqueIdentityMetadata = dict[str, str]


def normalize_object_path(value: str, *, allow_prefix: bool = False) -> str:
    """Return an unchanged canonical relative POSIX path or raise."""

    if (
        not value
        or value != value.strip()
        or value.startswith("/")
        or "\\" in value
        or "\x00" in value
        or (not allow_prefix and value.endswith("/"))
    ):
        raise ValueError("object path must be an unchanged relative POSIX path")
    candidate = value[:-1] if allow_prefix and value.endswith("/") else value
    if not candidate:
        raise ValueError("object path must not be empty")
    if any(part in {"", ".", ".."} for part in candidate.split("/")):
        raise ValueError("object path contains a forbidden segment")
    return value


def _canonical_metadata(value: dict[str, str]) -> dict[str, str]:
    if len(value) > _MAX_IDENTITY_METADATA_ITEMS:
        raise ValueError("identity metadata has too many entries")
    normalized: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key).casefold()
        item = str(raw_value)
        if _METADATA_KEY_PATTERN.fullmatch(key) is None:
            raise ValueError("identity metadata contains an invalid key")
        if not item or "\x00" in item:
            raise ValueError("identity metadata values must be nonempty and NUL-free")
        if key in normalized:
            raise ValueError("identity metadata keys collide after case folding")
        normalized[key] = item
    if sum(len(key.encode()) + len(item.encode()) for key, item in normalized.items()) > (
        _MAX_IDENTITY_METADATA_BYTES
    ):
        raise ValueError("identity metadata exceeds its encoded-size bound")
    return dict(sorted(normalized.items()))


class StorageAdapterModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AdapterDescriptor(StorageAdapterModel):
    protocol: Literal["riverhog-storage-adapter/v1"] = STORAGE_ADAPTER_PROTOCOL
    implementation_id: SemanticId
    implementation_version: str = Field(min_length=1, max_length=120)
    read_mode: ReadMode
    minimum_nonfinal_segment_bytes: int = Field(ge=1)
    maximum_segment_bytes: int | None = Field(default=None, ge=1)
    maximum_segment_count: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_segment_limits(self) -> Self:
        if (
            self.maximum_segment_bytes is not None
            and self.minimum_nonfinal_segment_bytes > self.maximum_segment_bytes
        ):
            raise ValueError("minimum non-final segment bytes exceed maximum segment bytes")
        return self


class ObjectLocator(StorageAdapterModel):
    object_path: str = Field(min_length=1, max_length=4096)
    revision: str | None = Field(default=None, min_length=1, max_length=2000)

    @field_validator("object_path")
    @classmethod
    def canonical_path(cls, value: str) -> str:
        return normalize_object_path(value)


class WriteSession(StorageAdapterModel):
    object_path: str = Field(min_length=1, max_length=4096)
    write_token: str = Field(min_length=1, max_length=4000)

    @field_validator("object_path")
    @classmethod
    def canonical_path(cls, value: str) -> str:
        return normalize_object_path(value)


class WriteStartRequest(StorageAdapterModel):
    object_path: str = Field(min_length=1, max_length=4096)
    content_type: str = Field(min_length=1, max_length=255)
    identity_metadata: OpaqueIdentityMetadata
    placement: ObjectPlacement

    @field_validator("object_path")
    @classmethod
    def canonical_path(cls, value: str) -> str:
        return normalize_object_path(value)

    @field_validator("identity_metadata")
    @classmethod
    def canonical_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        return _canonical_metadata(value)


class WriteSegmentReceipt(StorageAdapterModel):
    number: int = Field(ge=1)
    segment_token: str = Field(min_length=1, max_length=4000)
    stored_bytes: int = Field(ge=1)
    stored_sha256: Sha256 | None = None


class WriteSegmentRequest(StorageAdapterModel):
    session: WriteSession
    number: int = Field(ge=1)
    stored_bytes: int = Field(ge=1)


class WriteCompleteRequest(StorageAdapterModel):
    session: WriteSession
    segments: tuple[WriteSegmentReceipt, ...] = Field(min_length=1)
    expected_bytes: int = Field(ge=1)
    expected_identity_metadata: OpaqueIdentityMetadata
    expected_placement: ObjectPlacement

    @field_validator("segments")
    @classmethod
    def canonical_segments(
        cls,
        value: tuple[WriteSegmentReceipt, ...],
    ) -> tuple[WriteSegmentReceipt, ...]:
        if [segment.number for segment in value] != list(range(1, len(value) + 1)):
            raise ValueError("write segments must be contiguous and ordered from one")
        return value

    @field_validator("expected_identity_metadata")
    @classmethod
    def canonical_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        return _canonical_metadata(value)

    @model_validator(mode="after")
    def validate_bytes(self) -> Self:
        if sum(segment.stored_bytes for segment in self.segments) != self.expected_bytes:
            raise ValueError("write byte count does not equal its segments")
        return self


class CompletedWriteLookupRequest(StorageAdapterModel):
    object_path: str = Field(min_length=1, max_length=4096)
    expected_identity_metadata: OpaqueIdentityMetadata
    expected_placement: ObjectPlacement

    @field_validator("object_path")
    @classmethod
    def canonical_path(cls, value: str) -> str:
        return normalize_object_path(value)

    @field_validator("expected_identity_metadata")
    @classmethod
    def canonical_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        return _canonical_metadata(value)


class CompletedObjectReceipt(StorageAdapterModel):
    object_path: str = Field(min_length=1, max_length=4096)
    revision: str | None = Field(default=None, min_length=1, max_length=2000)
    entity_token: str | None = Field(default=None, min_length=1, max_length=4000)
    stored_bytes: int = Field(ge=1)
    completed_at: str = Field(min_length=1, max_length=100)

    @field_validator("object_path")
    @classmethod
    def canonical_path(cls, value: str) -> str:
        return normalize_object_path(value)


class SmallObjectWriteRequest(StorageAdapterModel):
    object_path: str = Field(min_length=1, max_length=4096)
    content_type: str = Field(min_length=1, max_length=255)
    identity_metadata: OpaqueIdentityMetadata
    placement: ObjectPlacement
    mode: Literal["create_only", "replace_current"]
    stored_bytes: int = Field(ge=0)
    stored_sha256: Sha256

    @field_validator("object_path")
    @classmethod
    def canonical_path(cls, value: str) -> str:
        return normalize_object_path(value)

    @field_validator("identity_metadata")
    @classmethod
    def canonical_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        return _canonical_metadata(value)


class ImmutableObjectReceipt(StorageAdapterModel):
    object_path: str = Field(min_length=1, max_length=4096)
    revision: str | None = Field(default=None, min_length=1, max_length=2000)
    entity_token: str | None = Field(default=None, min_length=1, max_length=4000)
    stored_bytes: int = Field(ge=0)
    stored_sha256: Sha256
    completed_at: str = Field(min_length=1, max_length=100)

    @field_validator("object_path")
    @classmethod
    def canonical_path(cls, value: str) -> str:
        return normalize_object_path(value)


class ObjectMetadataReceipt(StorageAdapterModel):
    object_path: str = Field(min_length=1, max_length=4096)
    revision: str | None = Field(default=None, min_length=1, max_length=2000)
    entity_token: str | None = Field(default=None, min_length=1, max_length=4000)
    content_type: str | None = Field(default=None, min_length=1, max_length=255)
    stored_bytes: int = Field(ge=0)
    stored_sha256: Sha256 | None = None
    identity_metadata: OpaqueIdentityMetadata
    completed_at: str = Field(min_length=1, max_length=100)

    @field_validator("object_path")
    @classmethod
    def canonical_path(cls, value: str) -> str:
        return normalize_object_path(value)

    @field_validator("identity_metadata")
    @classmethod
    def canonical_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        return _canonical_metadata(value)


class ObjectHeadRequest(StorageAdapterModel):
    object: ObjectLocator
    expected_placement: ObjectPlacement


class ObjectReadRequest(StorageAdapterModel):
    object: ObjectLocator
    expected_bytes: int = Field(ge=0)
    offset: int | None = Field(default=None, ge=0)
    size: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if (self.offset is None) != (self.size is None):
            raise ValueError("object range requires both offset and size")
        if self.offset is not None and self.size is not None:
            if self.offset + self.size > self.expected_bytes:
                raise ValueError("object range exceeds the expected object bytes")
        return self


class DeleteObjectRequest(StorageAdapterModel):
    object: ObjectLocator
    mode: Literal["current", "exact_revision", "all_versions"]

    @model_validator(mode="after")
    def validate_revision(self) -> Self:
        if self.mode == "exact_revision" and self.object.revision is None:
            raise ValueError("exact revision deletion requires a revision")
        if self.mode != "exact_revision" and self.object.revision is not None:
            raise ValueError("current/all-version deletion must not name a revision")
        return self


class DeletePrefixRequest(StorageAdapterModel):
    object_prefix: str = Field(min_length=1, max_length=4096)
    mode: Literal["all_versions"] = "all_versions"

    @field_validator("object_prefix")
    @classmethod
    def canonical_prefix(cls, value: str) -> str:
        return normalize_object_path(value, allow_prefix=True)


class ReadPreparationRequest(StorageAdapterModel):
    objects: tuple[ObjectLocator, ...] = Field(min_length=1)

    @field_validator("objects")
    @classmethod
    def canonical_objects(cls, value: tuple[ObjectLocator, ...]) -> tuple[ObjectLocator, ...]:
        identities = [(item.object_path, item.revision or "") for item in value]
        if identities != sorted(identities) or len(identities) != len(set(identities)):
            raise ValueError("read objects must be unique and canonically ordered")
        return value


class ReadStatus(StorageAdapterModel):
    state: ReadState
    ready_at: str | None = Field(default=None, max_length=100)
    expires_at: str | None = Field(default=None, max_length=100)


class AbortIncompleteWritesRequest(StorageAdapterModel):
    object_prefix: str = Field(min_length=1, max_length=4096)
    initiated_before: str = Field(min_length=1, max_length=100)

    @field_validator("object_prefix")
    @classmethod
    def canonical_prefix(cls, value: str) -> str:
        return normalize_object_path(value, allow_prefix=True)


class StorageAdapterErrorBody(StorageAdapterModel):
    code: StorageAdapterErrorCode
    message: str = Field(min_length=1, max_length=2000)


class StorageAdapterError(StorageAdapterModel):
    error: StorageAdapterErrorBody


class StorageAdapterRejection(RuntimeError):
    """Provider-neutral expected refusal from a transport-neutral adapter."""

    def __init__(self, code: StorageAdapterErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class MaintenanceResult(StorageAdapterModel):
    affected: int = Field(ge=0)


class StorageAdapterPort(Protocol):
    """Transport-neutral effects required from one configured adapter target."""

    def descriptor(self) -> AdapterDescriptor: ...

    def begin_write(self, request: WriteStartRequest) -> WriteSession: ...

    def write_segment(
        self,
        *,
        session: WriteSession,
        number: int,
        content: bytes,
    ) -> WriteSegmentReceipt: ...

    def list_segments(self, session: WriteSession) -> tuple[WriteSegmentReceipt, ...]: ...

    def complete_write(
        self,
        request: WriteCompleteRequest,
    ) -> CompletedObjectReceipt: ...

    def find_completed_write(
        self,
        request: CompletedWriteLookupRequest,
    ) -> CompletedObjectReceipt | None: ...

    def abort_write(self, session: WriteSession) -> None: ...

    def put_small_object(
        self,
        request: SmallObjectWriteRequest,
        content: bytes,
    ) -> ImmutableObjectReceipt: ...

    def head_object(self, request: ObjectHeadRequest) -> ObjectMetadataReceipt | None: ...

    def iter_object(self, request: ObjectReadRequest) -> Iterator[bytes]: ...

    def delete_object(self, request: DeleteObjectRequest) -> None: ...

    def delete_prefix(self, request: DeletePrefixRequest) -> int: ...

    def prepare_read(self, request: ReadPreparationRequest) -> ReadStatus: ...

    def read_status(self, request: ReadPreparationRequest) -> ReadStatus: ...

    def cleanup_read(self, request: ReadPreparationRequest) -> None: ...

    def abort_incomplete_writes(self, request: AbortIncompleteWritesRequest) -> int: ...


__all__ = [
    "STORAGE_ADAPTER_PROTOCOL",
    "AbortIncompleteWritesRequest",
    "AdapterDescriptor",
    "CompletedObjectReceipt",
    "DeleteObjectRequest",
    "DeletePrefixRequest",
    "ImmutableObjectReceipt",
    "MaintenanceResult",
    "WriteCompleteRequest",
    "WriteStartRequest",
    "CompletedWriteLookupRequest",
    "WriteSegmentReceipt",
    "WriteSegmentRequest",
    "WriteSession",
    "ObjectLocator",
    "ObjectHeadRequest",
    "ObjectMetadataReceipt",
    "ObjectPlacement",
    "ObjectReadRequest",
    "OpaqueIdentityMetadata",
    "ReadPreparationRequest",
    "ReadMode",
    "ReadState",
    "ReadStatus",
    "SemanticId",
    "Sha256",
    "SmallObjectWriteRequest",
    "StorageAdapterError",
    "StorageAdapterErrorBody",
    "StorageAdapterErrorCode",
    "StorageAdapterModel",
    "StorageAdapterPort",
    "StorageAdapterRejection",
    "normalize_object_path",
]

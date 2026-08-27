"""Provider-neutral object capabilities carried across the adapter boundary.

The models mirror Riverhog's existing object-store ports. They deliberately do
not define a transfer job, journal, archive object, collection, or provider
configuration model.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from hashlib import sha256
from typing import Annotated, Literal, Protocol, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)
from time_formats import format_utc_timestamp, parse_utc_timestamp

STORAGE_ADAPTER_PROTOCOL: Literal["riverhog-storage-adapter/v1"] = "riverhog-storage-adapter/v1"
ADAPTER_PRIVATE_ASSERTION_PREFIX = "riverhog-adapter-"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SEMANTIC_ID_PATTERN = r"^[a-z0-9](?:[a-z0-9._/-]{0,158}[a-z0-9])?$"
_METADATA_KEY_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
_MAX_IDENTITY_ASSERTIONS_ITEMS = 64
_MAX_IDENTITY_ASSERTIONS_BYTES = 16 * 1024

Sha256 = Annotated[str, StringConstraints(pattern=_SHA256_PATTERN)]
SemanticId = Annotated[str, StringConstraints(pattern=_SEMANTIC_ID_PATTERN)]
ObjectPlacement = Literal["archive", "immediate"]
ReadMode = Literal["immediate", "restore_required"]
StorageAdapterErrorCode = Literal[
    "unauthorized",
    "invalid_request",
    "not_found",
    "method_not_allowed",
    "length_required",
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
RequiredIdentityAssertions = Annotated[
    dict[str, str],
    Field(
        description=(
            "Inert caller-owned facts used only to identify and reconcile an exact stored "
            "object. Adapters canonicalize, persist, return, and compare these assertions; "
            "they must not interpret them as routing, retrieval, retention, credentials, "
            "placement, or provider-control instructions. Adapters may retain additional "
            "adapter-private assertions."
        )
    ),
]
BinaryContent = bytes | Iterable[bytes]


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


def _canonical_identity_assertions(value: dict[str, str]) -> dict[str, str]:
    if len(value) > _MAX_IDENTITY_ASSERTIONS_ITEMS:
        raise ValueError("required identity assertions have too many entries")
    normalized: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key).casefold()
        item = str(raw_value)
        if _METADATA_KEY_PATTERN.fullmatch(key) is None:
            raise ValueError("required identity assertions contain an invalid key")
        if key.startswith(ADAPTER_PRIVATE_ASSERTION_PREFIX):
            raise ValueError("required identity assertions use the adapter-private namespace")
        if not item or "\x00" in item:
            raise ValueError("required identity assertion values must be nonempty and NUL-free")
        if key in normalized:
            raise ValueError("required identity assertion keys collide after case folding")
        normalized[key] = item
    if sum(len(key.encode()) + len(item.encode()) for key, item in normalized.items()) > (
        _MAX_IDENTITY_ASSERTIONS_BYTES
    ):
        raise ValueError("required identity assertions exceed their encoded-size bound")
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
    write_token: str = Field(
        min_length=1,
        max_length=4000,
        description=(
            "Opaque adapter-owned persistable continuation handle. For the same configured "
            "adapter it remains replayable across client, transport, Riverhog, and adapter "
            "process restarts until completion, explicit abort, or caller-authorized "
            "incomplete-write reclamation makes the write terminal."
        ),
    )

    @field_validator("object_path")
    @classmethod
    def canonical_path(cls, value: str) -> str:
        return normalize_object_path(value)


class WriteStartRequest(StorageAdapterModel):
    object_path: str = Field(min_length=1, max_length=4096)
    content_type: str = Field(min_length=1, max_length=255)
    required_identity_assertions: RequiredIdentityAssertions
    placement: ObjectPlacement

    @field_validator("object_path")
    @classmethod
    def canonical_path(cls, value: str) -> str:
        return normalize_object_path(value)

    @field_validator("required_identity_assertions")
    @classmethod
    def canonical_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        return _canonical_identity_assertions(value)


class WriteSegmentReceipt(StorageAdapterModel):
    number: int = Field(ge=1)
    segment_token: str = Field(min_length=1, max_length=4000)
    stored_bytes: int = Field(ge=1)
    stored_sha256: Sha256 | None = None


def _listed_segments(
    value: tuple[WriteSegmentReceipt, ...],
) -> tuple[WriteSegmentReceipt, ...]:
    numbers = [segment.number for segment in value]
    if numbers != sorted(numbers) or len(numbers) != len(set(numbers)):
        raise ValueError("listed write segments must be unique and strictly ordered")
    return value


def _completion_segments(
    value: tuple[WriteSegmentReceipt, ...],
) -> tuple[WriteSegmentReceipt, ...]:
    if [segment.number for segment in value] != list(range(1, len(value) + 1)):
        raise ValueError("write segments must be contiguous and ordered from one")
    return value


def _canonical_utc_timestamp(value: str) -> str:
    try:
        parsed = parse_utc_timestamp(value)
    except ValueError as exc:
        raise ValueError("timestamp must include UTC context") from exc
    if format_utc_timestamp(parsed) != value:
        raise ValueError("timestamp must use the canonical UTC representation")
    return value


class WriteSegmentSet(StorageAdapterModel):
    session: WriteSession
    segments: tuple[WriteSegmentReceipt, ...] = ()

    @field_validator("segments")
    @classmethod
    def canonical_segments(
        cls,
        value: tuple[WriteSegmentReceipt, ...],
    ) -> tuple[WriteSegmentReceipt, ...]:
        return _listed_segments(value)


class WriteSegmentRequest(StorageAdapterModel):
    session: WriteSession
    number: int = Field(ge=1)
    stored_bytes: int = Field(ge=1)


class WriteCompleteRequest(StorageAdapterModel):
    session: WriteSession
    segments: tuple[WriteSegmentReceipt, ...] = Field(min_length=1)
    expected_bytes: int = Field(ge=1)
    expected_content_type: str = Field(min_length=1, max_length=255)
    required_identity_assertions: RequiredIdentityAssertions
    expected_placement: ObjectPlacement

    @field_validator("segments")
    @classmethod
    def canonical_segments(
        cls,
        value: tuple[WriteSegmentReceipt, ...],
    ) -> tuple[WriteSegmentReceipt, ...]:
        return _completion_segments(value)

    @field_validator("required_identity_assertions")
    @classmethod
    def canonical_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        return _canonical_identity_assertions(value)

    @model_validator(mode="after")
    def validate_bytes(self) -> Self:
        if sum(segment.stored_bytes for segment in self.segments) != self.expected_bytes:
            raise ValueError("write byte count does not equal its segments")
        return self


class CompletedWriteLookupRequest(StorageAdapterModel):
    object_path: str = Field(min_length=1, max_length=4096)
    expected_content_type: str = Field(min_length=1, max_length=255)
    required_identity_assertions: RequiredIdentityAssertions
    expected_placement: ObjectPlacement

    @field_validator("object_path")
    @classmethod
    def canonical_path(cls, value: str) -> str:
        return normalize_object_path(value)

    @field_validator("required_identity_assertions")
    @classmethod
    def canonical_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        return _canonical_identity_assertions(value)


class CompletedObjectReceipt(StorageAdapterModel):
    object_path: str = Field(min_length=1, max_length=4096)
    revision: str | None = Field(default=None, min_length=1, max_length=2000)
    entity_token: str | None = Field(default=None, min_length=1, max_length=4000)
    stored_bytes: int = Field(ge=1)
    verified_content_type: str = Field(min_length=1, max_length=255)
    verified_identity_assertions: RequiredIdentityAssertions
    verified_placement: ObjectPlacement
    completed_at: str = Field(min_length=1, max_length=100)

    @field_validator("object_path")
    @classmethod
    def canonical_path(cls, value: str) -> str:
        return normalize_object_path(value)

    @field_validator("verified_identity_assertions")
    @classmethod
    def canonical_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        return _canonical_identity_assertions(value)

    @field_validator("completed_at")
    @classmethod
    def canonical_completed_at(cls, value: str) -> str:
        return _canonical_utc_timestamp(value)


class SmallObjectWriteRequest(StorageAdapterModel):
    object_path: str = Field(min_length=1, max_length=4096)
    content_type: str = Field(min_length=1, max_length=255)
    required_identity_assertions: RequiredIdentityAssertions
    placement: ObjectPlacement
    mode: Literal["create_only", "replace_current"]
    stored_bytes: int = Field(ge=0)
    stored_sha256: Sha256

    @field_validator("object_path")
    @classmethod
    def canonical_path(cls, value: str) -> str:
        return normalize_object_path(value)

    @field_validator("required_identity_assertions")
    @classmethod
    def canonical_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        return _canonical_identity_assertions(value)


class ImmutableObjectReceipt(StorageAdapterModel):
    object_path: str = Field(min_length=1, max_length=4096)
    revision: str | None = Field(default=None, min_length=1, max_length=2000)
    entity_token: str | None = Field(default=None, min_length=1, max_length=4000)
    stored_bytes: int = Field(ge=0)
    stored_sha256: Sha256
    verified_content_type: str = Field(min_length=1, max_length=255)
    verified_identity_assertions: RequiredIdentityAssertions
    verified_placement: ObjectPlacement
    completed_at: str = Field(min_length=1, max_length=100)

    @field_validator("object_path")
    @classmethod
    def canonical_path(cls, value: str) -> str:
        return normalize_object_path(value)

    @field_validator("verified_identity_assertions")
    @classmethod
    def canonical_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        return _canonical_identity_assertions(value)

    @field_validator("completed_at")
    @classmethod
    def canonical_completed_at(cls, value: str) -> str:
        return _canonical_utc_timestamp(value)


class ObjectMetadataReceipt(StorageAdapterModel):
    object_path: str = Field(min_length=1, max_length=4096)
    revision: str | None = Field(default=None, min_length=1, max_length=2000)
    entity_token: str | None = Field(default=None, min_length=1, max_length=4000)
    content_type: str | None = Field(default=None, min_length=1, max_length=255)
    stored_bytes: int = Field(ge=0)
    stored_sha256: Sha256 | None = None
    observed_identity_assertions: RequiredIdentityAssertions
    verified_placement: ObjectPlacement
    completed_at: str = Field(min_length=1, max_length=100)

    @field_validator("object_path")
    @classmethod
    def canonical_path(cls, value: str) -> str:
        return normalize_object_path(value)

    @field_validator("observed_identity_assertions")
    @classmethod
    def canonical_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        return _canonical_identity_assertions(value)

    @field_validator("completed_at")
    @classmethod
    def canonical_completed_at(cls, value: str) -> str:
        return _canonical_utc_timestamp(value)


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


class ReadRequested(StorageAdapterModel):
    state: Literal["requested"] = "requested"
    estimated_ready_at: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("estimated_ready_at")
    @classmethod
    def canonical_estimated_ready_at(cls, value: str | None) -> str | None:
        return _canonical_utc_timestamp(value) if value is not None else None


class ReadReady(StorageAdapterModel):
    state: Literal["ready"] = "ready"
    available_until: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("available_until")
    @classmethod
    def canonical_available_until(cls, value: str | None) -> str | None:
        return _canonical_utc_timestamp(value) if value is not None else None


class ReadExpired(StorageAdapterModel):
    state: Literal["expired"] = "expired"


ReadReadiness = Annotated[
    ReadRequested | ReadReady | ReadExpired,
    Field(discriminator="state"),
]


class ReadStatus(StorageAdapterModel):
    objects: tuple[ObjectLocator, ...] = Field(min_length=1)
    readiness: ReadReadiness

    @field_validator("objects")
    @classmethod
    def canonical_objects(cls, value: tuple[ObjectLocator, ...]) -> tuple[ObjectLocator, ...]:
        return ReadPreparationRequest(objects=value).objects


class AbortIncompleteWritesRequest(StorageAdapterModel):
    object_prefix: str = Field(min_length=1, max_length=4096)
    initiated_before: str = Field(
        min_length=1,
        max_length=100,
        description=("Caller-authorized cutoff that terminalizes matching nonterminal writes."),
    )

    @field_validator("object_prefix")
    @classmethod
    def canonical_prefix(cls, value: str) -> str:
        return normalize_object_path(value, allow_prefix=True)

    @field_validator("initiated_before")
    @classmethod
    def canonical_initiated_before(cls, value: str) -> str:
        return _canonical_utc_timestamp(value)


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


def validate_write_session_response(
    request: WriteStartRequest,
    response: WriteSession,
) -> None:
    if response.object_path != request.object_path:
        raise ValueError("adapter write session differs from its request")


def validate_write_segment_response(
    request: WriteSegmentRequest,
    response: WriteSegmentReceipt,
) -> None:
    if response.number != request.number or response.stored_bytes != request.stored_bytes:
        raise ValueError("adapter segment receipt differs from its request")


def validate_write_segment_set_response(
    request: WriteSession,
    response: WriteSegmentSet,
    descriptor: AdapterDescriptor,
) -> None:
    if response.session != request:
        raise ValueError("adapter segment set differs from its write session")
    if descriptor.maximum_segment_count is not None and any(
        segment.number > descriptor.maximum_segment_count for segment in response.segments
    ):
        raise ValueError("adapter segment set exceeds its advertised segment-count limit")


def validate_completed_write_response(
    request: WriteCompleteRequest | CompletedWriteLookupRequest,
    response: CompletedObjectReceipt,
) -> None:
    expected_path = (
        request.session.object_path
        if isinstance(request, WriteCompleteRequest)
        else request.object_path
    )
    if response.object_path != expected_path:
        raise ValueError("adapter completed-object receipt differs from its request")
    if response.verified_identity_assertions != request.required_identity_assertions:
        raise ValueError("adapter completed-object identity assertions differ from their request")
    if response.verified_content_type != request.expected_content_type:
        raise ValueError("adapter completed-object content type differs from its request")
    if response.verified_placement != request.expected_placement:
        raise ValueError("adapter completed-object placement differs from its request")
    if (
        isinstance(request, WriteCompleteRequest)
        and response.stored_bytes != request.expected_bytes
    ):
        raise ValueError("adapter completed-object bytes differ from their request")


def validate_small_object_response(
    request: SmallObjectWriteRequest,
    response: ImmutableObjectReceipt,
) -> None:
    if (
        response.object_path != request.object_path
        or response.stored_bytes != request.stored_bytes
        or response.stored_sha256 != request.stored_sha256
        or response.verified_content_type != request.content_type
        or response.verified_identity_assertions != request.required_identity_assertions
        or response.verified_placement != request.placement
    ):
        raise ValueError("adapter immutable-object receipt differs from its request")


def validate_object_metadata_response(
    request: ObjectHeadRequest,
    response: ObjectMetadataReceipt,
) -> None:
    if response.object_path != request.object.object_path:
        raise ValueError("adapter object metadata differs from its request")
    if request.object.revision is not None and response.revision != request.object.revision:
        raise ValueError("adapter object metadata revision differs from its request")
    if response.verified_placement != request.expected_placement:
        raise ValueError("adapter object metadata placement differs from its request")


def validate_read_status_response(
    request: ReadPreparationRequest,
    response: ReadStatus,
) -> None:
    if response.objects != request.objects:
        raise ValueError("adapter read status differs from its request")


class StorageAdapterPort(Protocol):
    """Transport-neutral effects required from one configured adapter target.

    Public identity assertions are inert reconciliation evidence. Write sessions are durable
    continuation identities rather than process-local handles; provider-specific state needed
    to honor them remains private to the adapter.
    """

    def descriptor(self) -> AdapterDescriptor: ...

    def begin_write(self, request: WriteStartRequest) -> WriteSession: ...

    def write_segment(
        self,
        *,
        session: WriteSession,
        number: int,
        stored_bytes: int,
        content: BinaryContent,
    ) -> WriteSegmentReceipt: ...

    def list_segments(self, session: WriteSession) -> WriteSegmentSet: ...

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
        content: BinaryContent,
    ) -> ImmutableObjectReceipt: ...

    def head_object(self, request: ObjectHeadRequest) -> ObjectMetadataReceipt | None: ...

    def iter_object(self, request: ObjectReadRequest) -> Iterator[bytes]: ...

    def delete_object(self, request: DeleteObjectRequest) -> None: ...

    def delete_prefix(self, request: DeletePrefixRequest) -> int: ...

    def prepare_read(self, request: ReadPreparationRequest) -> ReadStatus: ...

    def read_status(self, request: ReadPreparationRequest) -> ReadStatus: ...

    def cleanup_read(self, request: ReadPreparationRequest) -> None: ...

    def abort_incomplete_writes(self, request: AbortIncompleteWritesRequest) -> int: ...


class _ContentValidation:
    def __init__(self, content: BinaryContent, *, expected_bytes: int) -> None:
        self._content = content
        self.expected_bytes = expected_bytes
        self.observed_bytes = 0
        self.digest = sha256()
        self.exhausted = False
        self.started = False

    def content(self) -> BinaryContent:
        if isinstance(self._content, bytes):
            if self._content:
                self._observe(self._content)
            self.exhausted = True
            return self._content
        return self._iter_content()

    def _observe(self, chunk: bytes) -> None:
        if not isinstance(chunk, bytes) or not chunk:
            raise ValueError("adapter content chunks must be nonempty bytes")
        self.observed_bytes += len(chunk)
        if self.observed_bytes > self.expected_bytes:
            raise ValueError("adapter content exceeds its declared byte count")
        self.digest.update(chunk)

    def _iter_content(self) -> Iterator[bytes]:
        if self.started:
            raise ValueError("adapter content may be consumed only once")
        self.started = True
        for chunk in cast(Iterable[bytes], self._content):
            self._observe(chunk)
            yield chunk
        self.exhausted = True

    def require_complete(self) -> None:
        if not self.exhausted or self.observed_bytes != self.expected_bytes:
            raise ValueError("adapter did not consume the declared content bytes")


def _response[ModelT: StorageAdapterModel](
    value: object,
    expected: type[ModelT],
    label: str,
) -> ModelT:
    if not isinstance(value, expected):
        raise TypeError(f"adapter returned an invalid {label}")
    return value


class ValidatedStorageAdapterPort:
    """One exact response-acceptance domain for every transport-neutral adapter."""

    def __init__(self, adapter: StorageAdapterPort) -> None:
        self._adapter = adapter
        self._descriptor: AdapterDescriptor | None = None

    def descriptor(self) -> AdapterDescriptor:
        if self._descriptor is None:
            self._descriptor = _response(
                self._adapter.descriptor(),
                AdapterDescriptor,
                "descriptor",
            )
        return self._descriptor

    def begin_write(self, request: WriteStartRequest) -> WriteSession:
        response = _response(self._adapter.begin_write(request), WriteSession, "write session")
        validate_write_session_response(request, response)
        return response

    def write_segment(
        self,
        *,
        session: WriteSession,
        number: int,
        stored_bytes: int,
        content: BinaryContent,
    ) -> WriteSegmentReceipt:
        request = WriteSegmentRequest(
            session=session,
            number=number,
            stored_bytes=stored_bytes,
        )
        descriptor = self.descriptor()
        if (
            descriptor.maximum_segment_bytes is not None
            and stored_bytes > descriptor.maximum_segment_bytes
        ) or (
            descriptor.maximum_segment_count is not None
            and number > descriptor.maximum_segment_count
        ):
            raise ValueError("write segment exceeds the adapter's advertised limits")
        validation = _ContentValidation(content, expected_bytes=stored_bytes)
        response = _response(
            self._adapter.write_segment(
                session=session,
                number=number,
                stored_bytes=stored_bytes,
                content=validation.content(),
            ),
            WriteSegmentReceipt,
            "write segment receipt",
        )
        validation.require_complete()
        validate_write_segment_response(request, response)
        if (
            response.stored_sha256 is not None
            and response.stored_sha256 != validation.digest.hexdigest()
        ):
            raise ValueError("adapter segment digest differs from the supplied content")
        return response

    def list_segments(self, session: WriteSession) -> WriteSegmentSet:
        response = _response(
            self._adapter.list_segments(session),
            WriteSegmentSet,
            "write segment set",
        )
        validate_write_segment_set_response(session, response, self.descriptor())
        return response

    def complete_write(self, request: WriteCompleteRequest) -> CompletedObjectReceipt:
        response = _response(
            self._adapter.complete_write(request),
            CompletedObjectReceipt,
            "completed-object receipt",
        )
        validate_completed_write_response(request, response)
        return response

    def find_completed_write(
        self,
        request: CompletedWriteLookupRequest,
    ) -> CompletedObjectReceipt | None:
        response = self._adapter.find_completed_write(request)
        if response is None:
            return None
        validated = _response(response, CompletedObjectReceipt, "completed-object receipt")
        validate_completed_write_response(request, validated)
        return validated

    def abort_write(self, session: WriteSession) -> None:
        self._adapter.abort_write(session)

    def put_small_object(
        self,
        request: SmallObjectWriteRequest,
        content: BinaryContent,
    ) -> ImmutableObjectReceipt:
        validation = _ContentValidation(content, expected_bytes=request.stored_bytes)
        response = _response(
            self._adapter.put_small_object(request, validation.content()),
            ImmutableObjectReceipt,
            "immutable-object receipt",
        )
        validation.require_complete()
        if validation.digest.hexdigest() != request.stored_sha256:
            raise ValueError("small-object content digest differs from its request")
        validate_small_object_response(request, response)
        return response

    def head_object(self, request: ObjectHeadRequest) -> ObjectMetadataReceipt | None:
        response = self._adapter.head_object(request)
        if response is None:
            return None
        validated = _response(response, ObjectMetadataReceipt, "object metadata receipt")
        validate_object_metadata_response(request, validated)
        return validated

    def iter_object(self, request: ObjectReadRequest) -> Iterator[bytes]:
        expected = request.size if request.size is not None else request.expected_bytes
        observed = 0
        for chunk in self._adapter.iter_object(request):
            if not isinstance(chunk, bytes) or not chunk:
                raise ValueError("adapter read chunks must be nonempty bytes")
            observed += len(chunk)
            if observed > expected:
                raise ValueError("adapter read exceeds the requested byte count")
            yield chunk
        if observed != expected:
            raise ValueError("adapter read differs from the requested byte count")

    def delete_object(self, request: DeleteObjectRequest) -> None:
        self._adapter.delete_object(request)

    def delete_prefix(self, request: DeletePrefixRequest) -> int:
        return self._affected(self._adapter.delete_prefix(request), "delete-prefix")

    def prepare_read(self, request: ReadPreparationRequest) -> ReadStatus:
        response = _response(
            self._adapter.prepare_read(request),
            ReadStatus,
            "read status",
        )
        validate_read_status_response(request, response)
        return response

    def read_status(self, request: ReadPreparationRequest) -> ReadStatus:
        response = _response(
            self._adapter.read_status(request),
            ReadStatus,
            "read status",
        )
        validate_read_status_response(request, response)
        return response

    def cleanup_read(self, request: ReadPreparationRequest) -> None:
        self._adapter.cleanup_read(request)

    def abort_incomplete_writes(self, request: AbortIncompleteWritesRequest) -> int:
        return self._affected(
            self._adapter.abort_incomplete_writes(request),
            "abort-incomplete-writes",
        )

    @staticmethod
    def _affected(value: object, operation: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TypeError(f"adapter {operation} response must be a nonnegative integer")
        return value


def validated_storage_adapter(adapter: StorageAdapterPort) -> ValidatedStorageAdapterPort:
    """Return the shared validation boundary without nesting it."""

    if isinstance(adapter, ValidatedStorageAdapterPort):
        return adapter
    return ValidatedStorageAdapterPort(adapter)


__all__ = [
    "ADAPTER_PRIVATE_ASSERTION_PREFIX",
    "STORAGE_ADAPTER_PROTOCOL",
    "AbortIncompleteWritesRequest",
    "AdapterDescriptor",
    "BinaryContent",
    "CompletedObjectReceipt",
    "DeleteObjectRequest",
    "DeletePrefixRequest",
    "ImmutableObjectReceipt",
    "MaintenanceResult",
    "WriteCompleteRequest",
    "WriteStartRequest",
    "CompletedWriteLookupRequest",
    "WriteSegmentReceipt",
    "WriteSegmentSet",
    "WriteSegmentRequest",
    "WriteSession",
    "ObjectLocator",
    "ObjectHeadRequest",
    "ObjectMetadataReceipt",
    "ObjectPlacement",
    "ObjectReadRequest",
    "RequiredIdentityAssertions",
    "ReadPreparationRequest",
    "ReadMode",
    "ReadExpired",
    "ReadReadiness",
    "ReadReady",
    "ReadRequested",
    "ReadStatus",
    "SemanticId",
    "Sha256",
    "SmallObjectWriteRequest",
    "StorageAdapterError",
    "StorageAdapterErrorBody",
    "StorageAdapterErrorCode",
    "StorageAdapterModel",
    "StorageAdapterPort",
    "ValidatedStorageAdapterPort",
    "StorageAdapterRejection",
    "normalize_object_path",
    "validate_completed_write_response",
    "validate_object_metadata_response",
    "validate_read_status_response",
    "validate_small_object_response",
    "validate_write_segment_response",
    "validate_write_segment_set_response",
    "validate_write_session_response",
    "validated_storage_adapter",
]

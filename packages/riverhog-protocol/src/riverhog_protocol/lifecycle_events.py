"""Typed Riverhog lifecycle vocabulary over the generic CloudEvents envelope."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from lifecycle_events.models import CloudEvent, EventContext, normalize_event_context
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

from riverhog_protocol.paths import CollectionId, normalize_collection_id
from riverhog_protocol.storage_names import ArchiveStoreName

RIVERHOG_EVENT_TYPE_PREFIX = "io.riverhog.riverhog."
COLLECTION_FINALIZED = RIVERHOG_EVENT_TYPE_PREFIX + "collection.finalized"
COLLECTION_DELETED = RIVERHOG_EVENT_TYPE_PREFIX + "collection.deleted"
ARCHIVE_COPY_REQUESTED = RIVERHOG_EVENT_TYPE_PREFIX + "archive_copy.requested"
ARCHIVE_COPY_COMPLETED = RIVERHOG_EVENT_TYPE_PREFIX + "archive_copy.completed"
ARCHIVE_COPY_ISSUE = RIVERHOG_EVENT_TYPE_PREFIX + "archive_copy.issue"
ARCHIVE_COPY_CANCELED = RIVERHOG_EVENT_TYPE_PREFIX + "archive_copy.canceled"
RETRIEVAL_REQUESTED = RIVERHOG_EVENT_TYPE_PREFIX + "retrieval.requested"
RETRIEVAL_READY = RIVERHOG_EVENT_TYPE_PREFIX + "retrieval.ready"
RETRIEVAL_RENEWED = RIVERHOG_EVENT_TYPE_PREFIX + "retrieval.renewed"
RETRIEVAL_COMPLETED = RIVERHOG_EVENT_TYPE_PREFIX + "retrieval.completed"
RETRIEVAL_CANCELED = RIVERHOG_EVENT_TYPE_PREFIX + "retrieval.canceled"
RETRIEVAL_EXPIRED = RIVERHOG_EVENT_TYPE_PREFIX + "retrieval.expired"
RETRIEVAL_ISSUE = RIVERHOG_EVENT_TYPE_PREFIX + "retrieval.issue"
RETRIEVAL_FAILED = RIVERHOG_EVENT_TYPE_PREFIX + "retrieval.failed"

RIVERHOG_EVENT_TYPES = frozenset(
    {
        COLLECTION_FINALIZED,
        COLLECTION_DELETED,
        ARCHIVE_COPY_REQUESTED,
        ARCHIVE_COPY_COMPLETED,
        ARCHIVE_COPY_ISSUE,
        ARCHIVE_COPY_CANCELED,
        RETRIEVAL_REQUESTED,
        RETRIEVAL_READY,
        RETRIEVAL_RENEWED,
        RETRIEVAL_COMPLETED,
        RETRIEVAL_CANCELED,
        RETRIEVAL_EXPIRED,
        RETRIEVAL_ISSUE,
        RETRIEVAL_FAILED,
    }
)
COLLECTION_WAKE_EVENT_TYPES = frozenset({COLLECTION_FINALIZED})
MAX_LIFECYCLE_EVENT_SEQUENCE = 2**63 - 1


def validate_lifecycle_event_cursor(value: str) -> str:
    if not value.isascii() or not value.isdecimal() or str(int(value)) != value:
        raise ValueError("lifecycle-event cursor must be canonical decimal")
    if int(value) > MAX_LIFECYCLE_EVENT_SEQUENCE:
        raise ValueError("lifecycle-event cursor exceeds the v1 sequence domain")
    return value


type LifecycleEventCursor = Annotated[
    str,
    StringConstraints(min_length=1, max_length=19, pattern=r"^(?:0|[1-9][0-9]*)$"),
    AfterValidator(validate_lifecycle_event_cursor),
]


class RiverhogEventModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    def __getitem__(self, key: str) -> Any:
        return self.model_dump(mode="json", exclude_none=True)[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.model_dump(mode="json", exclude_none=True).get(key, default)


class RiverhogActor(RiverhogEventModel):
    app: str = Field(min_length=1, max_length=160)
    key_id: str | None = Field(default=None, min_length=1, max_length=300)


class RiverhogEventCause(RiverhogEventModel):
    id: str = Field(min_length=1, max_length=300)
    source: str = Field(min_length=1, max_length=1000)
    type: str = Field(min_length=1, max_length=300)
    subject: str | None = Field(default=None, min_length=1, max_length=1000)


class RiverhogEventData(RiverhogEventModel):
    actor: RiverhogActor
    initiator: RiverhogActor
    cause: RiverhogEventCause | None = None
    context: EventContext | None = None

    @field_validator("context")
    @classmethod
    def validate_context(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return normalize_event_context(value)


class CollectionEventData(RiverhogEventData):
    collection_id: CollectionId
    collection_created_at: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_collection(self) -> Self:
        normalize_collection_id(self.collection_id)
        return self


class CollectionFinalizedData(CollectionEventData):
    files_total: int = Field(ge=0)
    bytes_total: int = Field(ge=0)
    archive_root_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CollectionDeletedData(CollectionEventData):
    files: int = Field(ge=0)
    bytes: int = Field(ge=0)
    remote_storage_bytes: int = Field(ge=0)


ArchiveCopyState = Literal[
    "requested",
    "waiting",
    "checking",
    "copying",
    "canceling",
    "completed",
    "failed",
    "canceled",
]


class ArchiveCopyEventData(CollectionEventData):
    source_store: ArchiveStoreName
    destination_store: ArchiveStoreName
    state: ArchiveCopyState


class ArchiveCopyRequestedData(ArchiveCopyEventData):
    state: Literal["requested"]


class ArchiveCopyCompletedData(ArchiveCopyEventData):
    state: Literal["completed"]


class ArchiveCopyIssueData(ArchiveCopyEventData):
    state: Literal["failed"]
    error: str = Field(min_length=1, max_length=16384)


class ArchiveCopyCanceledData(ArchiveCopyEventData):
    state: Literal["canceled"]


RetrievalState = Literal["requested", "ready", "completed", "canceled", "expired", "failed"]


class RetrievalEventData(RiverhogEventData):
    retrieval_id: str = Field(min_length=1, max_length=300)
    collection_ids: list[CollectionId] = Field(min_length=1)
    state: RetrievalState
    collection_id: CollectionId | None = None
    collection_created_at: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_collections(self) -> Self:
        ids = tuple(self.collection_ids)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("retrieval event collection identities must be canonical")
        if len(ids) == 1 and self.collection_id != ids[0]:
            raise ValueError("single-collection retrieval event requires its collection identity")
        if len(ids) != 1 and self.collection_id is not None:
            raise ValueError("multi-collection retrieval event cannot have a singular identity")
        if self.collection_created_at is not None and self.collection_id is None:
            raise ValueError("retrieval collection projection has no singular collection")
        return self


class RetrievalRequestedData(RetrievalEventData):
    state: Literal["requested", "ready"]
    files: int = Field(ge=1)
    objects: int = Field(ge=1)
    restore_required: bool


class RetrievalReadyData(RetrievalEventData):
    state: Literal["ready"]
    expires_at: str = Field(min_length=1, max_length=64)


class RetrievalRenewedData(RetrievalReadyData):
    pass


class RetrievalCompletedData(RetrievalEventData):
    state: Literal["completed"]


class RetrievalCanceledData(RetrievalEventData):
    state: Literal["canceled"]
    reason: str | None = Field(default=None, min_length=1, max_length=1000)


class RetrievalExpiredData(RetrievalEventData):
    state: Literal["expired"]


class RetrievalIssueData(RetrievalEventData):
    state: Literal["requested"]
    error: str = Field(min_length=1, max_length=16384)


class RetrievalFailedData(RetrievalEventData):
    state: Literal["failed"]
    error: str = Field(min_length=1, max_length=16384)


class RiverhogCloudEvent(CloudEvent):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    data: Any

    @model_validator(mode="after")
    def validate_subject_identity(self) -> Self:
        if isinstance(self.data, CollectionEventData):
            if self.subject != str(self.data.collection_id):
                raise ValueError("collection event subject differs from its collection identity")
        elif isinstance(self.data, RetrievalEventData):
            if self.subject != self.data.retrieval_id:
                raise ValueError("retrieval event subject differs from its retrieval identity")
        return self


class CollectionFinalizedEvent(RiverhogCloudEvent):
    type: Literal["io.riverhog.riverhog.collection.finalized"]
    data: CollectionFinalizedData


class CollectionDeletedEvent(RiverhogCloudEvent):
    type: Literal["io.riverhog.riverhog.collection.deleted"]
    data: CollectionDeletedData


class ArchiveCopyRequestedEvent(RiverhogCloudEvent):
    type: Literal["io.riverhog.riverhog.archive_copy.requested"]
    data: ArchiveCopyRequestedData


class ArchiveCopyCompletedEvent(RiverhogCloudEvent):
    type: Literal["io.riverhog.riverhog.archive_copy.completed"]
    data: ArchiveCopyCompletedData


class ArchiveCopyIssueEvent(RiverhogCloudEvent):
    type: Literal["io.riverhog.riverhog.archive_copy.issue"]
    data: ArchiveCopyIssueData


class ArchiveCopyCanceledEvent(RiverhogCloudEvent):
    type: Literal["io.riverhog.riverhog.archive_copy.canceled"]
    data: ArchiveCopyCanceledData


class RetrievalRequestedEvent(RiverhogCloudEvent):
    type: Literal["io.riverhog.riverhog.retrieval.requested"]
    data: RetrievalRequestedData


class RetrievalReadyEvent(RiverhogCloudEvent):
    type: Literal["io.riverhog.riverhog.retrieval.ready"]
    data: RetrievalReadyData


class RetrievalRenewedEvent(RiverhogCloudEvent):
    type: Literal["io.riverhog.riverhog.retrieval.renewed"]
    data: RetrievalRenewedData


class RetrievalCompletedEvent(RiverhogCloudEvent):
    type: Literal["io.riverhog.riverhog.retrieval.completed"]
    data: RetrievalCompletedData


class RetrievalCanceledEvent(RiverhogCloudEvent):
    type: Literal["io.riverhog.riverhog.retrieval.canceled"]
    data: RetrievalCanceledData


class RetrievalExpiredEvent(RiverhogCloudEvent):
    type: Literal["io.riverhog.riverhog.retrieval.expired"]
    data: RetrievalExpiredData


class RetrievalIssueEvent(RiverhogCloudEvent):
    type: Literal["io.riverhog.riverhog.retrieval.issue"]
    data: RetrievalIssueData


class RetrievalFailedEvent(RiverhogCloudEvent):
    type: Literal["io.riverhog.riverhog.retrieval.failed"]
    data: RetrievalFailedData


type RiverhogLifecycleEvent = Annotated[
    CollectionFinalizedEvent
    | CollectionDeletedEvent
    | ArchiveCopyRequestedEvent
    | ArchiveCopyCompletedEvent
    | ArchiveCopyIssueEvent
    | ArchiveCopyCanceledEvent
    | RetrievalRequestedEvent
    | RetrievalReadyEvent
    | RetrievalRenewedEvent
    | RetrievalCompletedEvent
    | RetrievalCanceledEvent
    | RetrievalExpiredEvent
    | RetrievalIssueEvent
    | RetrievalFailedEvent,
    Field(discriminator="type"),
]

_EVENT_ADAPTER: TypeAdapter[RiverhogLifecycleEvent] = TypeAdapter(RiverhogLifecycleEvent)


class RiverhogEventPage(RiverhogEventModel):
    events: list[RiverhogLifecycleEvent]
    next_cursor: LifecycleEventCursor
    has_more: bool

    def require_progress_after(self, cursor: str) -> None:
        if self.events and self.next_cursor == cursor:
            raise ValueError("nonempty lifecycle-event page did not advance its cursor")


def normalize_riverhog_event_type(value: str) -> str:
    normalized = (
        value
        if value.startswith(RIVERHOG_EVENT_TYPE_PREFIX)
        else (RIVERHOG_EVENT_TYPE_PREFIX + value)
    )
    if normalized not in RIVERHOG_EVENT_TYPES:
        raise ValueError(f"unknown Riverhog lifecycle event type: {value}")
    return normalized


def validate_riverhog_event(value: CloudEvent | dict[str, Any]) -> RiverhogLifecycleEvent:
    payload = (
        value.model_dump(mode="json", exclude_none=True) if isinstance(value, CloudEvent) else value
    )
    return _EVENT_ADAPTER.validate_python(payload)


def collection_id_for_event(value: CloudEvent | dict[str, Any]) -> int:
    event = validate_riverhog_event(value)
    if event.type not in COLLECTION_WAKE_EVENT_TYPES:
        raise ValueError("Riverhog event is not a collection wake event")
    if not isinstance(event.data, CollectionEventData):
        raise ValueError("Riverhog collection wake event has invalid data")
    return event.data.collection_id


__all__ = [
    "ARCHIVE_COPY_CANCELED",
    "ARCHIVE_COPY_COMPLETED",
    "ARCHIVE_COPY_ISSUE",
    "ARCHIVE_COPY_REQUESTED",
    "COLLECTION_DELETED",
    "COLLECTION_FINALIZED",
    "COLLECTION_WAKE_EVENT_TYPES",
    "RETRIEVAL_CANCELED",
    "RETRIEVAL_COMPLETED",
    "RETRIEVAL_EXPIRED",
    "RETRIEVAL_FAILED",
    "RETRIEVAL_ISSUE",
    "RETRIEVAL_READY",
    "RETRIEVAL_RENEWED",
    "RETRIEVAL_REQUESTED",
    "RIVERHOG_EVENT_TYPES",
    "RIVERHOG_EVENT_TYPE_PREFIX",
    "RiverhogActor",
    "RiverhogEventCause",
    "RiverhogEventPage",
    "RiverhogLifecycleEvent",
    "collection_id_for_event",
    "normalize_riverhog_event_type",
    "validate_riverhog_event",
]

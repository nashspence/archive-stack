"""Exact public projections for the Stove0 v1 operator API."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal, Self

from lifecycle_events import CloudEvent, cloud_event
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    field_validator,
    model_validator,
)
from stove0_observer_protocol import ObservationRequest, ObservationResult
from stove0_protocol import (
    ArtifactSubject,
    BranchSetPlan,
    ControllerEvidence,
    CoordinationSettlement,
    EvaluationDefinition,
    JoinPlan,
    Sha256,
    WorkflowPlan,
    WorkIdentity,
)
from stove0_recipe_config import RecipeDefinition
from stove0_target_protocol import (
    AcceptedTargetJob,
    OutputCollectionRef,
    TargetJobStatus,
    TargetPlan,
)

WorkPhase = Literal[
    "eligible",
    "claimed",
    "observing",
    "planning",
    "target_preflight",
    "queued",
    "executing",
    "output_finalizing",
    "verifying",
    "settled",
    "retirement_pending",
    "coordinating",
    "abandon_pending",
    "complete",
    "inapplicable",
    "failed",
    "canceled",
]
EvaluationPhase = Literal[
    "planning",
    "running",
    "partially_complete",
    "complete",
    "failed",
    "canceled",
]
EvaluationChildState = Literal[
    "pending",
    "active",
    "complete",
    "inapplicable",
    "failed",
    "canceled",
]
SchedulerRole = Literal["controller", "worker", "combined"]


class OperatorModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkflowPreviewIn(OperatorModel):
    recipe_id: str = Field(min_length=1, max_length=160)
    recipe_revision: int | None = Field(default=None, ge=1)
    collection_ids: tuple[int, ...] = Field(min_length=1)
    effective_intent: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("collection_ids")
    @classmethod
    def canonical_collections(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if value != tuple(sorted(set(value))) or any(item < 1 for item in value):
            raise ValueError("collection IDs must be positive, unique, and ordered")
        return value


class WorkCreateIn(WorkflowPreviewIn):
    preview_sha256: Sha256


class WorkCancelIn(OperatorModel):
    reason: str | None = Field(default=None, max_length=1000)


class EvaluationReviewIn(OperatorModel):
    rating: int | None = Field(default=None, ge=1, le=5)
    note: str | None = Field(default=None, max_length=4000)


class SchedulerRunIn(OperatorModel):
    role: SchedulerRole = "combined"
    event_limit: int = Field(default=100, ge=1, le=100)
    work_limit: int = Field(default=25, ge=1, le=100)


STOVE0_EVENT_SOURCE: Literal["urn:riverhog:stove0"] = "urn:riverhog:stove0"
WORK_CREATED: Literal["io.riverhog.stove0.work.created"] = "io.riverhog.stove0.work.created"
WORK_UPDATED: Literal["io.riverhog.stove0.work.updated"] = "io.riverhog.stove0.work.updated"
BRANCH_SET_ADMITTED: Literal["io.riverhog.stove0.branch-set.admitted"] = (
    "io.riverhog.stove0.branch-set.admitted"
)
JOIN_ADMITTED: Literal["io.riverhog.stove0.join.admitted"] = "io.riverhog.stove0.join.admitted"
EVALUATION_CREATED: Literal["io.riverhog.stove0.evaluation.created"] = (
    "io.riverhog.stove0.evaluation.created"
)
EVALUATION_UPDATED: Literal["io.riverhog.stove0.evaluation.updated"] = (
    "io.riverhog.stove0.evaluation.updated"
)
type Stove0EventType = Literal[
    "io.riverhog.stove0.work.created",
    "io.riverhog.stove0.work.updated",
    "io.riverhog.stove0.branch-set.admitted",
    "io.riverhog.stove0.join.admitted",
    "io.riverhog.stove0.evaluation.created",
    "io.riverhog.stove0.evaluation.updated",
]
STOVE0_EVENT_TYPES = frozenset(
    {
        WORK_CREATED,
        WORK_UPDATED,
        BRANCH_SET_ADMITTED,
        JOIN_ADMITTED,
        EVALUATION_CREATED,
        EVALUATION_UPDATED,
    }
)


class Stove0EventData(OperatorModel):
    def __getitem__(self, key: str) -> Any:
        return self.model_dump(mode="json", exclude_none=True)[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.model_dump(mode="json", exclude_none=True).get(key, default)


class WorkCreatedEventData(Stove0EventData):
    work_id: Sha256
    phase: WorkPhase
    parent_work_id: Sha256 | None = None
    branch_set_sha256: Sha256 | None = None
    join_plan_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def exact_parent_binding(self) -> Self:
        if self.parent_work_id is None:
            if self.branch_set_sha256 is not None or self.join_plan_sha256 is not None:
                raise ValueError("root work creation cannot declare branch or join identities")
        elif self.branch_set_sha256 is None:
            raise ValueError("child work creation requires its branch-set identity")
        return self


class WorkUpdatedEventData(Stove0EventData):
    work_id: Sha256
    phase: WorkPhase
    revision: int = Field(ge=2)


class BranchSetAdmittedEventData(WorkUpdatedEventData):
    branch_set_sha256: Sha256
    branch_count: int = Field(ge=1)
    admitted_work_count: int = Field(ge=1)


class JoinAdmittedEventData(WorkUpdatedEventData):
    branch_set_sha256: Sha256
    join_plan_sha256: Sha256
    join_work_id: Sha256


class EvaluationCreatedEventData(Stove0EventData):
    evaluation_id: Sha256
    phase: EvaluationPhase


class EvaluationUpdatedEventData(EvaluationCreatedEventData):
    revision: int = Field(ge=2)


class Stove0CloudEvent(CloudEvent):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source: Literal["urn:riverhog:stove0"]
    subject: str = Field(min_length=1)
    data: Any

    @model_validator(mode="after")
    def exact_subject(self) -> Self:
        identity = (
            self.data.evaluation_id
            if isinstance(self.data, EvaluationCreatedEventData)
            else self.data.work_id
        )
        if self.subject != identity:
            raise ValueError("Stove0 event subject differs from its data identity")
        return self


class WorkCreatedEvent(Stove0CloudEvent):
    type: Literal["io.riverhog.stove0.work.created"]
    data: WorkCreatedEventData


class WorkUpdatedEvent(Stove0CloudEvent):
    type: Literal["io.riverhog.stove0.work.updated"]
    data: WorkUpdatedEventData


class BranchSetAdmittedEvent(Stove0CloudEvent):
    type: Literal["io.riverhog.stove0.branch-set.admitted"]
    data: BranchSetAdmittedEventData


class JoinAdmittedEvent(Stove0CloudEvent):
    type: Literal["io.riverhog.stove0.join.admitted"]
    data: JoinAdmittedEventData


class EvaluationCreatedEvent(Stove0CloudEvent):
    type: Literal["io.riverhog.stove0.evaluation.created"]
    data: EvaluationCreatedEventData


class EvaluationUpdatedEvent(Stove0CloudEvent):
    type: Literal["io.riverhog.stove0.evaluation.updated"]
    data: EvaluationUpdatedEventData


type Stove0LifecycleEvent = Annotated[
    WorkCreatedEvent
    | WorkUpdatedEvent
    | BranchSetAdmittedEvent
    | JoinAdmittedEvent
    | EvaluationCreatedEvent
    | EvaluationUpdatedEvent,
    Field(discriminator="type"),
]

_STOVE0_EVENT_ADAPTER: TypeAdapter[Stove0LifecycleEvent] = TypeAdapter(Stove0LifecycleEvent)


def parse_stove0_event(value: object) -> Stove0LifecycleEvent:
    return _STOVE0_EVENT_ADAPTER.validate_python(value)


def stove0_event(
    *,
    type: Stove0EventType,
    subject: str,
    data: Mapping[str, Any],
) -> Stove0LifecycleEvent:
    event = cloud_event(
        source=STOVE0_EVENT_SOURCE,
        type=type,
        subject=subject,
        data=data,
    )
    return parse_stove0_event(event.model_dump(mode="python", exclude_none=True))


class Stove0EventPage(OperatorModel):
    events: list[Stove0LifecycleEvent]
    next_cursor: str
    has_more: bool

    def require_progress_after(self, cursor: str) -> None:
        if self.events and self.next_cursor == cursor:
            raise ValueError("nonempty Stove0 event page did not advance its cursor")


class WorkClaimView(OperatorModel):
    claim_id: str = Field(min_length=1, max_length=160)
    fence: int = Field(ge=1)


class WorkFailureView(OperatorModel):
    code: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=1000)
    retryable: bool


class WorkInapplicableView(OperatorModel):
    code: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=1000)


class PreviewTargetExpectationView(OperatorModel):
    branch_id: str = Field(min_length=1, max_length=160)
    work_id: Sha256
    plan_sha256: Sha256


class PreviewAcceptanceView(OperatorModel):
    preview_sha256: Sha256
    branch_set_sha256: Sha256
    target_plans: tuple[PreviewTargetExpectationView, ...]


class WorkView(OperatorModel):
    """Operator projection of mutable work; never an execution identity."""

    format: Literal["stove0-work-view/v1"] = "stove0-work-view/v1"
    work_id: Sha256
    work: WorkIdentity
    phase: WorkPhase
    revision: int = Field(ge=1)
    claim: WorkClaimView | None = None
    preview_acceptance: PreviewAcceptanceView | None = None
    expected_target_plan_sha256: Sha256 | None = None
    observation_requests: tuple[ObservationRequest, ...] = ()
    observation_results: tuple[ObservationResult, ...] = ()
    branch_set_plan: BranchSetPlan | None = None
    coordination_settlement: CoordinationSettlement | None = None
    join_plan: JoinPlan | None = None
    coordination_cancel_requested: bool = False
    workflow_plan: WorkflowPlan | None = None
    target_plan: TargetPlan | None = None
    controller_evidence: ControllerEvidence | None = None
    target_request: AcceptedTargetJob | None = None
    target_status: TargetJobStatus | None = None
    output: OutputCollectionRef | None = None
    retirement_remaining: tuple[int, ...] = ()
    failure: WorkFailureView | None = None
    inapplicable: WorkInapplicableView | None = None
    abandon_outcome: Literal["inapplicable", "failed", "canceled"] | None = None

    @model_validator(mode="after")
    def exact_identity(self) -> Self:
        if self.work_id != self.work.work_id:
            raise ValueError("work view ID differs from its immutable work identity")
        return self

    @classmethod
    def from_record(cls, record: BaseModel | Mapping[str, Any]) -> WorkView:
        payload = _payload(record)
        work = WorkIdentity.model_validate(payload.get("work"))
        payload.update(format="stove0-work-view/v1", work_id=work.work_id)
        return cls.model_validate(payload)


class WorkPage(OperatorModel):
    page: int = Field(ge=1)
    per_page: int = Field(ge=0)
    total: int = Field(ge=0)
    pages: int = Field(ge=0)
    sort: Literal["updated_at", "phase", "work_id"]
    order: Literal["asc", "desc"]
    filters: dict[str, JsonValue]
    work: tuple[WorkView, ...]

    @classmethod
    def from_page(cls, page: Mapping[str, Any]) -> WorkPage:
        payload = dict(page)
        payload["work"] = tuple(WorkView.from_record(item) for item in payload.get("work", ()))
        return cls.model_validate(payload)


class EvaluationChildView(OperatorModel):
    variant_id: str = Field(min_length=1, max_length=160)
    work_id: Sha256
    state: EvaluationChildState
    output: OutputCollectionRef | None = None


class EvaluationReviewView(OperatorModel):
    variant_id: str = Field(min_length=1, max_length=160)
    rating: int | None = Field(default=None, ge=1, le=5)
    note: str | None = Field(default=None, max_length=4000)
    updated_by: str = Field(min_length=1, max_length=160)
    updated_at: str = Field(min_length=1, max_length=40)


class EvaluationView(OperatorModel):
    """Operator projection of a materialized evaluation, not its identity."""

    format: Literal["stove0-evaluation-view/v1"] = "stove0-evaluation-view/v1"
    evaluation_id: Sha256
    definition: EvaluationDefinition
    phase: EvaluationPhase
    revision: int = Field(ge=1)
    children: tuple[EvaluationChildView, ...]
    reviews: tuple[EvaluationReviewView, ...] = ()

    @model_validator(mode="after")
    def exact_identity(self) -> Self:
        if self.evaluation_id != self.definition.evaluation_id:
            raise ValueError("evaluation view ID differs from its immutable definition")
        return self

    @classmethod
    def from_record(cls, record: BaseModel | Mapping[str, Any]) -> EvaluationView:
        payload = _payload(record)
        definition = EvaluationDefinition.model_validate(payload.get("definition"))
        payload.update(
            format="stove0-evaluation-view/v1",
            evaluation_id=definition.evaluation_id,
        )
        return cls.model_validate(payload)


class EvaluationPage(OperatorModel):
    page: int = Field(ge=1)
    per_page: int = Field(ge=0)
    total: int = Field(ge=0)
    pages: int = Field(ge=0)
    sort: Literal["updated_at", "phase", "evaluation_id"]
    order: Literal["asc", "desc"]
    filters: dict[str, JsonValue]
    evaluations: tuple[EvaluationView, ...]

    @classmethod
    def from_page(cls, page: Mapping[str, Any]) -> EvaluationPage:
        payload = dict(page)
        payload["evaluations"] = tuple(
            EvaluationView.from_record(item) for item in payload.get("evaluations", ())
        )
        return cls.model_validate(payload)


class RecipeView(OperatorModel):
    definition: RecipeDefinition
    sha256: Sha256

    @model_validator(mode="after")
    def exact_digest(self) -> Self:
        if self.sha256 != self.definition.sha256:
            raise ValueError("recipe view digest differs from its definition")
        return self

    @classmethod
    def from_definition(cls, definition: RecipeDefinition) -> RecipeView:
        return cls(definition=definition, sha256=definition.sha256)


class RecipeCatalogView(OperatorModel):
    catalog_sha256: Sha256
    recipes: tuple[RecipeView, ...]


class ArtifactSelectionPage(OperatorModel):
    page: int = Field(ge=1)
    per_page: int = Field(ge=0)
    total: int = Field(ge=0)
    pages: int = Field(ge=0)
    sort: Literal["id"] = "id"
    order: Literal["asc"] = "asc"
    filters: dict[str, JsonValue]
    selection_sha256: Sha256
    total_bytes: int = Field(ge=0)
    artifacts: tuple[ArtifactSubject, ...]


class SchedulerStatus(OperatorModel):
    running: bool
    interval_seconds: float = Field(gt=0)
    cursor: str
    roles: tuple[SchedulerRole, ...]


class SchedulerFailure(OperatorModel):
    event_id: str | None = None
    work_id: Sha256 | None = None
    error: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def one_subject(self) -> Self:
        if (self.event_id is None) == (self.work_id is None):
            raise ValueError("scheduler failure requires exactly one subject")
        return self


class SchedulerEventBatch(OperatorModel):
    events: int = Field(ge=0)
    next_cursor: str | None
    has_more: bool
    work_ids: tuple[Sha256, ...]
    failures: tuple[SchedulerFailure, ...] = ()


class SchedulerWorkBatch(OperatorModel):
    role: SchedulerRole
    cursor: str
    next_cursor: str
    progressed: tuple[Sha256, ...]
    failures: tuple[SchedulerFailure, ...]


class SchedulerPruning(OperatorModel):
    work: int = Field(ge=0)
    work_bytes: int = Field(ge=0)
    evaluations: int = Field(ge=0)
    evaluation_bytes: int = Field(ge=0)
    selections: int = Field(ge=0)
    selection_bytes: int = Field(ge=0)
    events: int = Field(ge=0)
    event_bytes: int = Field(ge=0)


class SchedulerRun(OperatorModel):
    pruning: SchedulerPruning | None
    events: SchedulerEventBatch
    work: SchedulerWorkBatch


def _payload(value: BaseModel | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python", by_alias=True, exclude_none=True)
    return dict(value)


__all__ = [
    "ArtifactSelectionPage",
    "BRANCH_SET_ADMITTED",
    "BranchSetAdmittedEvent",
    "BranchSetAdmittedEventData",
    "EvaluationChildView",
    "EVALUATION_CREATED",
    "EVALUATION_UPDATED",
    "EvaluationCreatedEvent",
    "EvaluationCreatedEventData",
    "EvaluationPhase",
    "EvaluationPage",
    "EvaluationReviewIn",
    "EvaluationReviewView",
    "EvaluationUpdatedEvent",
    "EvaluationUpdatedEventData",
    "EvaluationView",
    "JOIN_ADMITTED",
    "JoinAdmittedEvent",
    "JoinAdmittedEventData",
    "RecipeCatalogView",
    "RecipeView",
    "SchedulerEventBatch",
    "SchedulerFailure",
    "SchedulerPruning",
    "SchedulerRole",
    "SchedulerRun",
    "SchedulerRunIn",
    "SchedulerStatus",
    "SchedulerWorkBatch",
    "STOVE0_EVENT_SOURCE",
    "STOVE0_EVENT_TYPES",
    "Stove0CloudEvent",
    "Stove0EventData",
    "Stove0EventPage",
    "Stove0EventType",
    "Stove0LifecycleEvent",
    "WORK_CREATED",
    "WORK_UPDATED",
    "WorkCancelIn",
    "WorkClaimView",
    "WorkCreateIn",
    "WorkCreatedEvent",
    "WorkCreatedEventData",
    "WorkFailureView",
    "WorkInapplicableView",
    "WorkPage",
    "WorkPhase",
    "WorkUpdatedEvent",
    "WorkUpdatedEventData",
    "WorkView",
    "WorkflowPreviewIn",
    "parse_stove0_event",
    "stove0_event",
]

"""Canonical public state relationships and projections for Stove0 operators."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal, Self

from http_api_contracts import BrowsePageToken, CanonicalVisibleText
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
    ArtifactSelectionPage,
    BranchSetPlan,
    ControllerEvidence,
    CoordinationSettlement,
    EvaluationDefinition,
    JoinPlan,
    JoinWorkBinding,
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
    TargetSettlementAuthority,
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
SortOrder = Literal["asc", "desc"]
WorkSort = Literal["updated_at", "phase", "work_id"]
EvaluationSort = Literal["updated_at", "phase", "evaluation_id"]
SchedulerRole = Literal["controller", "worker", "combined"]


def validate_work_state_shape(
    *,
    work: WorkIdentity,
    phase: WorkPhase,
    claim: object | None,
    preview_acceptance: object | None,
    expected_target_plan_sha256: str | None,
    branch_set_plan: BranchSetPlan | None,
    coordination_settlement: CoordinationSettlement | None,
    join_plan: JoinPlan | None,
    coordination_cancel_requested: bool,
    workflow_plan: WorkflowPlan | None,
    output: OutputCollectionRef | None,
    retirement_remaining: Sequence[int],
    failure: object | None,
    inapplicable: object | None,
    abandon_outcome: Literal["inapplicable", "failed", "canceled"] | None,
) -> None:
    """Validate the one work-state relationship shared by storage and projection."""

    if phase == "eligible" and claim is not None:
        raise ValueError("eligible work cannot already hold a claim")
    inactive_without_claim = {"eligible", "failed", "canceled", "inapplicable"}
    if phase not in inactive_without_claim and claim is None:
        raise ValueError("active work phases require a claim")
    if output is not None and phase not in {
        "verifying",
        "settled",
        "retirement_pending",
        "abandon_pending",
        "complete",
    }:
        raise ValueError("output identity appears before verification")
    if phase in {"abandon_pending", "failed", "canceled", "inapplicable"} and output is not None:
        raise ValueError("non-success terminal work cannot contain an output")
    failure_phases = {"failed"}
    if phase == "abandon_pending" and abandon_outcome == "failed":
        failure_phases.add("abandon_pending")
    if phase in failure_phases and failure is None:
        raise ValueError("failed work requires failure details")
    if phase not in failure_phases and failure is not None:
        raise ValueError("only failed work may retain failure details")
    inapplicable_phases = {"inapplicable"}
    if phase == "abandon_pending" and abandon_outcome == "inapplicable":
        inapplicable_phases.add("abandon_pending")
    if phase in inapplicable_phases and inapplicable is None:
        raise ValueError("inapplicable work requires a terminal outcome")
    if phase not in inapplicable_phases and inapplicable is not None:
        raise ValueError("only inapplicable work may retain that outcome")
    if phase == "abandon_pending" and abandon_outcome is None:
        raise ValueError("abandon_pending work requires a terminal outcome")
    if phase != "abandon_pending" and abandon_outcome is not None:
        raise ValueError("only abandon_pending work may retain an abandon outcome")
    if retirement_remaining and phase != "retirement_pending":
        raise ValueError("retirement work must remain in retirement_pending")
    if branch_set_plan is not None:
        if branch_set_plan.parent_work != work:
            raise ValueError("branch-set plan differs from its parent work record")
        if workflow_plan is not None or isinstance(work.fork_join, JoinWorkBinding):
            raise ValueError("coordination parents cannot also be target work")
        if phase not in {
            "eligible",
            "claimed",
            "coordinating",
            "retirement_pending",
            "complete",
            "abandon_pending",
            "canceled",
            "failed",
            "inapplicable",
        }:
            raise ValueError("branch-set plan appears in an invalid coordination phase")
    if coordination_settlement is not None:
        if (
            branch_set_plan is None
            or coordination_settlement.work != work
            or coordination_settlement.branch_set_sha256 != branch_set_plan.branch_set_sha256
            or phase not in {"claimed", "coordinating", "retirement_pending", "complete"}
        ):
            raise ValueError("coordination settlement differs from its durable coordinator")
    if preview_acceptance is not None:
        preview_branch_set = getattr(preview_acceptance, "branch_set_sha256", None)
        if work.fork_join is not None:
            raise ValueError("only parent work may retain a preview acceptance")
        if branch_set_plan is not None and branch_set_plan.branch_set_sha256 != preview_branch_set:
            raise ValueError("admitted branch set differs from the accepted preview")
    if expected_target_plan_sha256 is not None and workflow_plan is None:
        raise ValueError("a target-plan expectation requires ordinary target work")
    if join_plan is not None and (
        branch_set_plan is None or join_plan.branch_set_sha256 != branch_set_plan.branch_set_sha256
    ):
        raise ValueError("resolved join plan differs from its branch set")
    if phase == "coordinating" and branch_set_plan is None:
        raise ValueError("coordinating work requires an immutable branch-set plan")
    if coordination_cancel_requested and (
        branch_set_plan is None
        or phase not in {"eligible", "claimed", "coordinating", "abandon_pending", "canceled"}
    ):
        raise ValueError("coordination cancellation belongs only to a branch-set parent")
    if workflow_plan is not None and workflow_plan.work != work:
        raise ValueError("workflow plan differs from its ordinary work record")


def validate_evaluation_child_shape(
    state: EvaluationChildState,
    output: OutputCollectionRef | None,
) -> None:
    if (state == "complete") != (output is not None):
        raise ValueError("only completed evaluation children may carry output identity")


def validate_evaluation_review_shape(rating: int | None, note: str | None) -> None:
    if rating is None and not (note and note.strip()):
        raise ValueError("evaluation review requires a rating or note")
    if note is not None and note != note.strip():
        raise ValueError("evaluation review note must be canonical")


def validate_evaluation_state_shape(
    definition: EvaluationDefinition,
    children: Sequence[object],
    reviews: Sequence[object],
) -> None:
    expected = tuple(
        (variant.id, definition.child_work(variant.id).work_id)
        for variant in definition.matrix.variants
    )
    actual = tuple((item.variant_id, item.work_id) for item in children)  # type: ignore[attr-defined]
    if actual != expected:
        raise ValueError("evaluation children differ from the immutable matrix")
    review_ids = [item.variant_id for item in reviews]  # type: ignore[attr-defined]
    if review_ids != sorted(review_ids) or len(review_ids) != len(set(review_ids)):
        raise ValueError("evaluation reviews must be unique and ordered by variant ID")
    if not set(review_ids).issubset({item.variant_id for item in children}):  # type: ignore[attr-defined]
        raise ValueError("evaluation review names an unknown variant")


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


class EvaluationReviewIn(OperatorModel):
    model_config = ConfigDict(
        json_schema_extra={
            "anyOf": [
                {"required": ["rating"], "properties": {"rating": {"type": "integer"}}},
                {
                    "required": ["note"],
                    "properties": {"note": {"type": "string"}},
                },
            ]
        }
    )

    rating: int | None = Field(default=None, ge=1, le=5)
    note: CanonicalVisibleText | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def meaningful(self) -> Self:
        validate_evaluation_review_shape(self.rating, self.note)
        return self


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
    phase: Literal["coordinating"]
    branch_set_sha256: Sha256
    branch_count: int = Field(ge=1)
    admitted_work_count: int = Field(ge=1)


class JoinAdmittedEventData(WorkUpdatedEventData):
    phase: Literal["coordinating"]
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
    target_settlement: TargetSettlementAuthority | None = None
    retirement_remaining: tuple[int, ...] = ()
    failure: WorkFailureView | None = None
    inapplicable: WorkInapplicableView | None = None
    abandon_outcome: Literal["inapplicable", "failed", "canceled"] | None = None

    @model_validator(mode="after")
    def exact_identity(self) -> Self:
        if self.work_id != self.work.work_id:
            raise ValueError("work view ID differs from its immutable work identity")
        validate_work_state_shape(
            work=self.work,
            phase=self.phase,
            claim=self.claim,
            preview_acceptance=self.preview_acceptance,
            expected_target_plan_sha256=self.expected_target_plan_sha256,
            branch_set_plan=self.branch_set_plan,
            coordination_settlement=self.coordination_settlement,
            join_plan=self.join_plan,
            coordination_cancel_requested=self.coordination_cancel_requested,
            workflow_plan=self.workflow_plan,
            output=self.output,
            retirement_remaining=self.retirement_remaining,
            failure=self.failure,
            inapplicable=self.inapplicable,
            abandon_outcome=self.abandon_outcome,
        )
        return self

    @classmethod
    def from_record(cls, record: BaseModel | Mapping[str, Any]) -> WorkView:
        payload = _payload(record)
        work = WorkIdentity.model_validate(payload.get("work"))
        payload.update(format="stove0-work-view/v1", work_id=work.work_id)
        return cls.model_validate(payload)


class WorkPage(OperatorModel):
    page_size: int = Field(ge=1, le=100)
    next_page_token: BrowsePageToken | None
    sort: WorkSort
    order: SortOrder
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

    @model_validator(mode="after")
    def exact_output(self) -> Self:
        validate_evaluation_child_shape(self.state, self.output)
        return self


class EvaluationReviewView(OperatorModel):
    variant_id: str = Field(min_length=1, max_length=160)
    rating: int | None = Field(default=None, ge=1, le=5)
    note: CanonicalVisibleText | None = Field(default=None, max_length=4000)
    updated_by: str = Field(min_length=1, max_length=160)
    updated_at: str = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def meaningful(self) -> Self:
        validate_evaluation_review_shape(self.rating, self.note)
        return self


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
        validate_evaluation_state_shape(self.definition, self.children, self.reviews)
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
    page_size: int = Field(ge=1, le=100)
    next_page_token: BrowsePageToken | None
    sort: EvaluationSort
    order: SortOrder
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
    "EvaluationSort",
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
    "SortOrder",
    "Stove0CloudEvent",
    "Stove0EventData",
    "Stove0EventPage",
    "Stove0EventType",
    "Stove0LifecycleEvent",
    "WORK_CREATED",
    "WORK_UPDATED",
    "WorkClaimView",
    "WorkCreateIn",
    "WorkCreatedEvent",
    "WorkCreatedEventData",
    "WorkFailureView",
    "WorkInapplicableView",
    "WorkPage",
    "WorkPhase",
    "WorkSort",
    "WorkUpdatedEvent",
    "WorkUpdatedEventData",
    "WorkView",
    "WorkflowPreviewIn",
    "parse_stove0_event",
    "stove0_event",
    "validate_evaluation_child_shape",
    "validate_evaluation_review_shape",
    "validate_evaluation_state_shape",
    "validate_work_state_shape",
]

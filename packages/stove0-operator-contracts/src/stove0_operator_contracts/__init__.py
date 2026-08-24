"""Exact public projections for the Stove0 v1 operator API."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator
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
    "EvaluationChildView",
    "EvaluationPage",
    "EvaluationReviewView",
    "EvaluationView",
    "RecipeCatalogView",
    "RecipeView",
    "SchedulerEventBatch",
    "SchedulerFailure",
    "SchedulerPruning",
    "SchedulerRun",
    "SchedulerStatus",
    "SchedulerWorkBatch",
    "WorkClaimView",
    "WorkFailureView",
    "WorkInapplicableView",
    "WorkPage",
    "WorkPhase",
    "WorkView",
]

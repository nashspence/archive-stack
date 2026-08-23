"""Pure fork/join declaration and evaluation contracts for Stove0.

The module is transport-neutral and performs no I/O. It declares one exact set of
required ordinary child works and, optionally, one exact final join. Every child
and join remains an ordinary Stove0 work with a complete ``WorkflowPlan``; target
execution, persistence, scheduling, Riverhog verification, admission policy, and
retirement enforcement remain outside this kernel.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal, Self

from pydantic import Field, JsonValue, field_validator, model_validator

from stove0_protocol.jcs import canonical_json_bytes, canonical_json_sha256
from stove0_protocol.models import (
    WORKFLOW_PREVIEW_FORMAT,
    ArtifactSubject,
    BranchWorkBinding,
    CollectionRootRef,
    JoinWorkBinding,
    JoinWorkMemberBinding,
    ObservationEvidence,
    PreviewOutcome,
    RecipeRef,
    RetirementPolicy,
    SemanticId,
    Sha256,
    Stove0ProtocolModel,
    TargetPlanBinding,
    WorkflowPlan,
    WorkflowPlanIntent,
    WorkIdentity,
    WorkPayload,
)

ARTIFACT_SELECTION_FORMAT: Literal["stove0-artifact-selection/v1"] = "stove0-artifact-selection/v1"
BRANCH_SET_FORMAT: Literal["stove0-branch-set/v1"] = "stove0-branch-set/v1"
JOIN_DECLARATION_FORMAT: Literal["stove0-join-declaration/v1"] = "stove0-join-declaration/v1"
JOIN_PLAN_FORMAT: Literal["stove0-join-plan/v1"] = "stove0-join-plan/v1"
BRANCH_SETTLEMENT_FORMAT: Literal["stove0-branch-settlement/v1"] = "stove0-branch-settlement/v1"
JOIN_SETTLEMENT_FORMAT: Literal["stove0-join-settlement/v1"] = "stove0-join-settlement/v1"
BRANCH_OUTCOME_FORMAT: Literal["stove0-branch-outcome/v1"] = "stove0-branch-outcome/v1"
JOIN_OUTCOME_FORMAT: Literal["stove0-join-outcome/v1"] = "stove0-join-outcome/v1"

BranchOutcomeState = Literal["failed", "inapplicable", "interrupted", "canceled"]
JoinOutcomeState = Literal["failed", "inapplicable", "interrupted", "canceled"]
JoinEvaluationState = Literal[
    "not-declared",
    "waiting",
    "ready",
    "succeeded",
    "failed",
    "inapplicable",
    "interrupted",
    "canceled",
]


def _without_digest(model: Stove0ProtocolModel, field: str) -> dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True, exclude={field}, exclude_none=True)


def _root_key(root: CollectionRootRef) -> tuple[int, str, str]:
    return root.collection_id, root.manifest_sha256, root.content_identity


def _artifact_key(artifact: ArtifactSubject) -> tuple[str, int, str, str]:
    return (
        artifact.id,
        artifact.collection.collection_id,
        artifact.path,
        artifact.sha256,
    )


class ArtifactSelection(Stove0ProtocolModel):
    """One exact, content-addressed selection of immutable artifacts."""

    format: Literal["stove0-artifact-selection/v1"] = ARTIFACT_SELECTION_FORMAT
    artifacts: tuple[ArtifactSubject, ...] = Field(min_length=1)
    artifact_count: int = Field(ge=1)
    total_bytes: int = Field(ge=0)
    selection_sha256: Sha256

    @field_validator("artifacts")
    @classmethod
    def canonical_artifacts(cls, value: tuple[ArtifactSubject, ...]) -> tuple[ArtifactSubject, ...]:
        ordered = tuple(sorted(value, key=_artifact_key))
        if value != ordered:
            raise ValueError("selection artifacts must be canonically ordered")
        ids = [item.id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("selection artifact IDs must be unique")
        exact = [(_root_key(item.collection), item.path) for item in value]
        if len(exact) != len(set(exact)):
            raise ValueError("selection cannot repeat an exact collection artifact")
        roots: dict[int, CollectionRootRef] = {}
        for item in value:
            current = roots.setdefault(item.collection.collection_id, item.collection)
            if current != item.collection:
                raise ValueError("selection contains conflicting roots for one collection ID")
        return value

    @model_validator(mode="after")
    def verify_summary_and_digest(self) -> Self:
        if self.artifact_count != len(self.artifacts):
            raise ValueError("selection artifact count does not match its artifacts")
        if self.total_bytes != sum(item.bytes for item in self.artifacts):
            raise ValueError("selection byte count does not match its artifacts")
        expected = canonical_json_sha256(_without_digest(self, "selection_sha256"))
        if expected != self.selection_sha256:
            raise ValueError("selection digest does not match its canonical payload")
        return self

    @classmethod
    def seal(cls, artifacts: Sequence[ArtifactSubject]) -> ArtifactSelection:
        ordered = tuple(sorted(tuple(artifacts), key=_artifact_key))
        payload = {
            "format": ARTIFACT_SELECTION_FORMAT,
            "artifacts": [item.model_dump(mode="json", exclude_none=True) for item in ordered],
            "artifact_count": len(ordered),
            "total_bytes": sum(item.bytes for item in ordered),
        }
        return cls.model_validate({**payload, "selection_sha256": canonical_json_sha256(payload)})

    def ref(self) -> ArtifactSelectionRef:
        return ArtifactSelectionRef(
            selection_sha256=self.selection_sha256,
            artifact_count=self.artifact_count,
            total_bytes=self.total_bytes,
        )

    def roots(self) -> tuple[CollectionRootRef, ...]:
        values = {_root_key(item.collection): item.collection for item in self.artifacts}
        return tuple(values[key] for key in sorted(values))

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json", exclude_none=True))


class ArtifactSelectionRef(Stove0ProtocolModel):
    """Closed reference to a separately retained selection document."""

    selection_sha256: Sha256
    artifact_count: int = Field(ge=1)
    total_bytes: int = Field(ge=0)

    @classmethod
    def from_selection(cls, selection: ArtifactSelection) -> ArtifactSelectionRef:
        return selection.ref()


SelectionDocuments = Mapping[str, ArtifactSelection]


def resolve_selection(
    reference: ArtifactSelectionRef,
    selections: SelectionDocuments,
) -> ArtifactSelection:
    selection = selections.get(reference.selection_sha256)
    if selection is None:
        raise ValueError(f"artifact selection is unavailable: {reference.selection_sha256}")
    if selection.selection_sha256 != reference.selection_sha256:
        raise ValueError("selection document is stored under a mismatched digest")
    if (
        selection.artifact_count != reference.artifact_count
        or selection.total_bytes != reference.total_bytes
    ):
        raise ValueError("selection reference summary does not match its document")
    return selection


class BranchPlan(Stove0ProtocolModel):
    """One named required child work using the ordinary WorkflowPlan contract."""

    branch_id: SemanticId
    artifact_selection: ArtifactSelectionRef
    workflow_plan: WorkflowPlan

    @model_validator(mode="after")
    def bind_child_work(self) -> Self:
        if self.workflow_plan.retirement_policy != "retain":
            raise ValueError("branch workflow plans must retain their source collections")
        binding = self.workflow_plan.work.fork_join
        if not isinstance(binding, BranchWorkBinding):
            raise ValueError("branch workflow work requires an explicit branch binding")
        if (
            binding.branch_id != self.branch_id
            or binding.artifact_selection_sha256 != self.artifact_selection.selection_sha256
        ):
            raise ValueError("branch plan differs from its child work binding")
        return self

    @classmethod
    def build(
        cls,
        *,
        parent_work: WorkIdentity,
        branch_id: str,
        decision_sha256: str,
        selection: ArtifactSelection,
        recipe: RecipeRef,
        effective_intent: Mapping[str, JsonValue],
        workflow_intent: WorkflowPlanIntent,
        observations: tuple[ObservationEvidence, ...] = (),
    ) -> BranchPlan:
        child_work = WorkIdentity.seal(
            WorkPayload(
                recipe=recipe,
                inputs=selection.roots(),
                effective_intent=dict(effective_intent),
                evaluation=parent_work.evaluation,
                fork_join=BranchWorkBinding(
                    parent_work_id=parent_work.work_id,
                    branch_id=branch_id,
                    decision_sha256=decision_sha256,
                    artifact_selection_sha256=selection.selection_sha256,
                ),
            )
        )
        return cls(
            branch_id=branch_id,
            artifact_selection=selection.ref(),
            workflow_plan=workflow_intent.materialize(
                work=child_work,
                observations=observations,
            ),
        )


class JoinMemberDeclaration(Stove0ProtocolModel):
    """Exact named branch and opaque output roles required by the join."""

    branch_id: SemanticId
    output_roles: tuple[SemanticId, ...] = Field(min_length=1)

    @field_validator("output_roles")
    @classmethod
    def canonical_roles(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(value)) or len(value) != len(set(value)):
            raise ValueError("join output roles must be unique and ordered")
        return value


class JoinDeclaration(Stove0ProtocolModel):
    """One optional exact named-subset join declaration."""

    format: Literal["stove0-join-declaration/v1"] = JOIN_DECLARATION_FORMAT
    members: tuple[JoinMemberDeclaration, ...] = Field(min_length=2)
    recipe: RecipeRef
    effective_intent: dict[str, JsonValue] = Field(default_factory=dict)
    workflow_intent: WorkflowPlanIntent
    join_declaration_sha256: Sha256

    @field_validator("members")
    @classmethod
    def canonical_members(
        cls, value: tuple[JoinMemberDeclaration, ...]
    ) -> tuple[JoinMemberDeclaration, ...]:
        ids = [item.branch_id for item in value]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("join members must be unique and ordered by branch ID")
        return value

    @model_validator(mode="after")
    def verify_contract(self) -> Self:
        if self.workflow_intent.retirement_policy != "retain":
            raise ValueError("join workflow plans must retain every branch collection")
        expected = canonical_json_sha256(_without_digest(self, "join_declaration_sha256"))
        if expected != self.join_declaration_sha256:
            raise ValueError("join declaration digest does not match its canonical payload")
        return self

    @classmethod
    def seal(
        cls,
        *,
        members: Sequence[JoinMemberDeclaration],
        recipe: RecipeRef,
        effective_intent: Mapping[str, JsonValue],
        workflow_intent: WorkflowPlanIntent,
    ) -> JoinDeclaration:
        ordered = tuple(sorted(tuple(members), key=lambda item: item.branch_id))
        payload = {
            "format": JOIN_DECLARATION_FORMAT,
            "members": [item.model_dump(mode="json") for item in ordered],
            "recipe": recipe.model_dump(mode="json"),
            "effective_intent": dict(effective_intent),
            "workflow_intent": workflow_intent.model_dump(mode="json", exclude_none=True),
        }
        return cls.model_validate(
            {**payload, "join_declaration_sha256": canonical_json_sha256(payload)}
        )


class BranchSetPlan(Stove0ProtocolModel):
    """One immutable set of required branches and one optional exact join."""

    format: Literal["stove0-branch-set/v1"] = BRANCH_SET_FORMAT
    parent_work: WorkIdentity
    decision_sha256: Sha256
    evidence_sha256s: tuple[Sha256, ...] = ()
    branches: tuple[BranchPlan, ...] = Field(min_length=1)
    join: JoinDeclaration | None = None
    retirement_policy: RetirementPolicy = "retain"
    retirement_grace_seconds: int = Field(default=0, ge=0)
    branch_set_sha256: Sha256

    @field_validator("evidence_sha256s")
    @classmethod
    def canonical_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(value)) or len(value) != len(set(value)):
            raise ValueError("branch-set evidence identities must be unique and ordered")
        return value

    @field_validator("branches")
    @classmethod
    def canonical_branches(cls, value: tuple[BranchPlan, ...]) -> tuple[BranchPlan, ...]:
        ids = [item.branch_id for item in value]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("branches must be unique and ordered by branch ID")
        return value

    @model_validator(mode="after")
    def verify_contract(self) -> Self:
        if self.parent_work.fork_join is not None:
            raise ValueError("a branch-set parent must be top-level work")
        for branch in self.branches:
            binding = branch.workflow_plan.work.fork_join
            if not isinstance(binding, BranchWorkBinding):
                raise ValueError("branch work is missing its branch binding")
            if (
                binding.parent_work_id != self.parent_work.work_id
                or binding.decision_sha256 != self.decision_sha256
            ):
                raise ValueError("branch work differs from its parent decision")
        if self.join is not None:
            known = {item.branch_id for item in self.branches}
            unknown = [item.branch_id for item in self.join.members if item.branch_id not in known]
            if unknown:
                raise ValueError("join references unknown branches: " + ", ".join(unknown))
        if self.parent_work.evaluation is not None and self.retirement_policy != "retain":
            raise ValueError("evaluation-bound branch sets must retain their source collections")
        if self.retirement_policy == "retain" and self.retirement_grace_seconds:
            raise ValueError("retained branch sets cannot declare a retirement grace period")
        expected = canonical_json_sha256(_without_digest(self, "branch_set_sha256"))
        if expected != self.branch_set_sha256:
            raise ValueError("branch-set digest does not match its canonical payload")
        return self

    @classmethod
    def seal(
        cls,
        *,
        parent_work: WorkIdentity,
        decision_sha256: str,
        evidence_sha256s: Sequence[str] = (),
        branches: Sequence[BranchPlan],
        join: JoinDeclaration | None = None,
        retirement_policy: RetirementPolicy = "retain",
        retirement_grace_seconds: int = 0,
        selections: SelectionDocuments,
    ) -> BranchSetPlan:
        ordered_branches = tuple(sorted(tuple(branches), key=lambda item: item.branch_id))
        payload = {
            "format": BRANCH_SET_FORMAT,
            "parent_work": parent_work.model_dump(mode="json", by_alias=True, exclude_none=True),
            "decision_sha256": decision_sha256,
            "evidence_sha256s": sorted(set(evidence_sha256s)),
            "branches": [
                item.model_dump(mode="json", by_alias=True, exclude_none=True)
                for item in ordered_branches
            ],
            "retirement_policy": retirement_policy,
            "retirement_grace_seconds": retirement_grace_seconds,
        }
        if join is not None:
            payload["join"] = join.model_dump(mode="json", by_alias=True, exclude_none=True)
        plan = cls.model_validate({**payload, "branch_set_sha256": canonical_json_sha256(payload)})
        validate_branch_set_plan(plan, selections)
        return plan

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json", exclude_none=True))


class BranchSetDecision(Stove0ProtocolModel):
    """A sealed branch-set plan plus its separately addressable selections."""

    plan: BranchSetPlan
    selections: tuple[ArtifactSelection, ...]

    @field_validator("selections")
    @classmethod
    def canonical_selections(
        cls, value: tuple[ArtifactSelection, ...]
    ) -> tuple[ArtifactSelection, ...]:
        ids = [item.selection_sha256 for item in value]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("branch-set selections must be unique and ordered by digest")
        return value

    @model_validator(mode="after")
    def complete_documents(self) -> Self:
        referenced = {branch.artifact_selection.selection_sha256 for branch in self.plan.branches}
        if referenced != set(self.selection_documents):
            raise ValueError("branch-set decision must retain every exact selection once")
        validate_branch_set_plan(self.plan, self.selection_documents)
        return self

    @property
    def selection_documents(self) -> dict[str, ArtifactSelection]:
        return {item.selection_sha256: item for item in self.selections}


class BranchSettlement(Stove0ProtocolModel):
    """Success-only, Riverhog-verified result of one branch workflow plan."""

    format: Literal["stove0-branch-settlement/v1"] = BRANCH_SETTLEMENT_FORMAT
    branch_id: SemanticId
    work_id: Sha256
    workflow_plan_sha256: Sha256
    derivation_sha256: Sha256
    output_collection: CollectionRootRef
    output_selection: ArtifactSelectionRef
    settlement_sha256: Sha256

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        expected = canonical_json_sha256(_without_digest(self, "settlement_sha256"))
        if expected != self.settlement_sha256:
            raise ValueError("branch settlement digest does not match its canonical payload")
        return self

    @classmethod
    def seal(
        cls,
        *,
        branch: BranchPlan,
        derivation_sha256: str,
        output_collection: CollectionRootRef,
        output_selection: ArtifactSelection,
    ) -> BranchSettlement:
        payload = {
            "format": BRANCH_SETTLEMENT_FORMAT,
            "branch_id": branch.branch_id,
            "work_id": branch.workflow_plan.work.work_id,
            "workflow_plan_sha256": branch.workflow_plan.workflow_plan_sha256,
            "derivation_sha256": derivation_sha256,
            "output_collection": output_collection.model_dump(mode="json"),
            "output_selection": output_selection.ref().model_dump(mode="json"),
        }
        return cls.model_validate({**payload, "settlement_sha256": canonical_json_sha256(payload)})


class BranchOutcome(Stove0ProtocolModel):
    """Current non-success projection for an ordinary branch work record."""

    format: Literal["stove0-branch-outcome/v1"] = BRANCH_OUTCOME_FORMAT
    branch_id: SemanticId
    work_id: Sha256
    workflow_plan_sha256: Sha256
    state: BranchOutcomeState


class JoinInputPlan(Stove0ProtocolModel):
    branch_id: SemanticId
    settlement_sha256: Sha256
    derivation_sha256: Sha256
    output_collection: CollectionRootRef
    artifact_selection: ArtifactSelectionRef


class JoinPlan(Stove0ProtocolModel):
    """Resolved ordinary join work over exact successful branch outputs."""

    format: Literal["stove0-join-plan/v1"] = JOIN_PLAN_FORMAT
    parent_work_id: Sha256
    branch_set_sha256: Sha256
    declaration: JoinDeclaration
    inputs: tuple[JoinInputPlan, ...] = Field(min_length=2)
    work: WorkIdentity
    workflow_plan: WorkflowPlan
    join_plan_sha256: Sha256

    @field_validator("inputs")
    @classmethod
    def canonical_inputs(cls, value: tuple[JoinInputPlan, ...]) -> tuple[JoinInputPlan, ...]:
        ids = [item.branch_id for item in value]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("join inputs must be unique and ordered by branch ID")
        return value

    @model_validator(mode="after")
    def verify_contract(self) -> Self:
        if self.workflow_plan.work != self.work:
            raise ValueError("join workflow plan does not bind the resolved join work")
        if self.workflow_plan.retirement_policy != "retain":
            raise ValueError("join workflow plan must retain branch collections")
        if WorkflowPlanIntent.from_plan(self.workflow_plan) != self.declaration.workflow_intent:
            raise ValueError("join workflow plan differs from its declaration")
        if tuple(item.branch_id for item in self.inputs) != tuple(
            item.branch_id for item in self.declaration.members
        ):
            raise ValueError("join inputs differ from the declared member set")
        binding = self.work.fork_join
        if not isinstance(binding, JoinWorkBinding):
            raise ValueError("join work requires an explicit join binding")
        expected_members = tuple(
            JoinWorkMemberBinding(
                branch_id=item.branch_id,
                settlement_sha256=item.settlement_sha256,
                artifact_selection_sha256=item.artifact_selection.selection_sha256,
            )
            for item in self.inputs
        )
        if (
            binding.parent_work_id != self.parent_work_id
            or binding.branch_set_sha256 != self.branch_set_sha256
            or binding.members != expected_members
        ):
            raise ValueError("join work binding differs from its exact inputs")
        expected_roots = tuple(
            sorted({item.output_collection for item in self.inputs}, key=_root_key)
        )
        if self.work.inputs != expected_roots:
            raise ValueError("join work roots differ from the resolved branch outputs")
        expected = canonical_json_sha256(_without_digest(self, "join_plan_sha256"))
        if expected != self.join_plan_sha256:
            raise ValueError("join-plan digest does not match its canonical payload")
        return self

    @classmethod
    def seal(
        cls,
        *,
        parent_work_id: str,
        branch_set_sha256: str,
        declaration: JoinDeclaration,
        inputs: Sequence[JoinInputPlan],
        work: WorkIdentity,
        workflow_plan: WorkflowPlan,
    ) -> JoinPlan:
        ordered = tuple(sorted(tuple(inputs), key=lambda item: item.branch_id))
        payload = {
            "format": JOIN_PLAN_FORMAT,
            "parent_work_id": parent_work_id,
            "branch_set_sha256": branch_set_sha256,
            "declaration": declaration.model_dump(mode="json", exclude_none=True),
            "inputs": [item.model_dump(mode="json") for item in ordered],
            "work": work.model_dump(mode="json", exclude_none=True),
            "workflow_plan": workflow_plan.model_dump(mode="json", exclude_none=True),
        }
        return cls.model_validate({**payload, "join_plan_sha256": canonical_json_sha256(payload)})


class JoinSettlement(Stove0ProtocolModel):
    """Success-only, Riverhog-verified result of one resolved join plan."""

    format: Literal["stove0-join-settlement/v1"] = JOIN_SETTLEMENT_FORMAT
    work_id: Sha256
    workflow_plan_sha256: Sha256
    join_plan_sha256: Sha256
    derivation_sha256: Sha256
    output_collection: CollectionRootRef
    output_selection: ArtifactSelectionRef
    settlement_sha256: Sha256

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        expected = canonical_json_sha256(_without_digest(self, "settlement_sha256"))
        if expected != self.settlement_sha256:
            raise ValueError("join settlement digest does not match its canonical payload")
        return self

    @classmethod
    def seal(
        cls,
        *,
        plan: JoinPlan,
        derivation_sha256: str,
        output_collection: CollectionRootRef,
        output_selection: ArtifactSelection,
    ) -> JoinSettlement:
        payload = {
            "format": JOIN_SETTLEMENT_FORMAT,
            "work_id": plan.work.work_id,
            "workflow_plan_sha256": plan.workflow_plan.workflow_plan_sha256,
            "join_plan_sha256": plan.join_plan_sha256,
            "derivation_sha256": derivation_sha256,
            "output_collection": output_collection.model_dump(mode="json"),
            "output_selection": output_selection.ref().model_dump(mode="json"),
        }
        return cls.model_validate({**payload, "settlement_sha256": canonical_json_sha256(payload)})


class JoinOutcome(Stove0ProtocolModel):
    """Current non-success projection for one resolved join work record."""

    format: Literal["stove0-join-outcome/v1"] = JOIN_OUTCOME_FORMAT
    work_id: Sha256
    workflow_plan_sha256: Sha256
    join_plan_sha256: Sha256
    state: JoinOutcomeState


class BranchSetEvaluation(Stove0ProtocolModel):
    """Entirely derived view over a plan and ordinary child/join results."""

    branch_set_sha256: Sha256
    succeeded_branches: tuple[BranchSettlement, ...]
    unsettled_branch_ids: tuple[SemanticId, ...]
    failed_branch_ids: tuple[SemanticId, ...]
    inapplicable_branch_ids: tuple[SemanticId, ...]
    interrupted_branch_ids: tuple[SemanticId, ...]
    canceled_branch_ids: tuple[SemanticId, ...]
    join_ready: bool
    resolved_join_plan: JoinPlan | None
    join_state: JoinEvaluationState
    join_settlement: JoinSettlement | None
    unsettled_work_ids: tuple[Sha256, ...]
    branch_set_succeeded: bool
    retirement_requested: bool
    coordination_complete_for_retirement: bool


class BranchTargetPreview(Stove0ProtocolModel):
    """Target-owned preflight evidence for one exact previewed branch."""

    branch_id: SemanticId
    workflow_plan_sha256: Sha256
    target_plan: TargetPlanBinding


class WorkflowPreviewPayload(Stove0ProtocolModel):
    """Side-effect-free preview of the branch plan execution will admit."""

    format: Literal["stove0-workflow-preview/v1"] = WORKFLOW_PREVIEW_FORMAT
    preview_id: Sha256
    state: Literal["ready", "inapplicable", "failed", "canceled"]
    work: WorkIdentity
    observations: tuple[ObservationEvidence, ...] = ()
    branch_set_plan: BranchSetPlan | None = None
    selections: tuple[ArtifactSelection, ...] = ()
    target_plans: tuple[BranchTargetPreview, ...] = ()
    outcome: PreviewOutcome | None = None
    warnings: tuple[str, ...] = ()

    @field_validator("observations")
    @classmethod
    def canonical_observations(
        cls, value: tuple[ObservationEvidence, ...]
    ) -> tuple[ObservationEvidence, ...]:
        ids = [item.request.request_id for item in value]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("preview observations must be unique and ordered")
        return value

    @field_validator("selections")
    @classmethod
    def canonical_selections(
        cls, value: tuple[ArtifactSelection, ...]
    ) -> tuple[ArtifactSelection, ...]:
        ids = [item.selection_sha256 for item in value]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("preview selections must be unique and ordered by digest")
        return value

    @field_validator("target_plans")
    @classmethod
    def canonical_target_plans(
        cls, value: tuple[BranchTargetPreview, ...]
    ) -> tuple[BranchTargetPreview, ...]:
        ids = [item.branch_id for item in value]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("preview target plans must be unique and ordered by branch ID")
        return value

    @field_validator("warnings")
    @classmethod
    def canonical_warnings(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(value)) or len(value) != len(set(value)):
            raise ValueError("preview warnings must be unique and ordered")
        return value

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.state == "ready":
            if self.branch_set_plan is None or self.outcome is not None:
                raise ValueError("ready preview requires a branch-set plan and no outcome")
            if self.branch_set_plan.parent_work != self.work:
                raise ValueError("preview branch-set plan differs from the requested work")
            documents = {item.selection_sha256: item for item in self.selections}
            validate_branch_set_plan(self.branch_set_plan, documents)
            branches = {item.branch_id: item for item in self.branch_set_plan.branches}
            if set(branches) != {item.branch_id for item in self.target_plans}:
                raise ValueError("ready preview requires one target plan for every branch")
            for target in self.target_plans:
                workflow = branches[target.branch_id].workflow_plan
                if target.workflow_plan_sha256 != workflow.workflow_plan_sha256:
                    raise ValueError("preview target plan binds another branch workflow plan")
                if (
                    target.target_plan.target_contract_sha256 != workflow.target_contract_sha256
                    or target.target_plan.operation_contract_sha256 != workflow.operation.sha256
                ):
                    raise ValueError("preview target plan differs from branch workflow selection")
        else:
            if self.branch_set_plan is not None or self.selections or self.target_plans:
                raise ValueError("non-ready preview cannot contain executable plans")
            if self.outcome is None:
                raise ValueError("non-ready preview requires an outcome")
        return self


class WorkflowPreview(WorkflowPreviewPayload):
    preview_sha256: Sha256

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        if canonical_json_sha256(_without_digest(self, "preview_sha256")) != self.preview_sha256:
            raise ValueError("workflow preview digest does not match its canonical result")
        return self

    @classmethod
    def seal(cls, payload: WorkflowPreviewPayload) -> WorkflowPreview:
        document = payload.model_dump(mode="json", by_alias=True, exclude_none=True)
        return cls(**document, preview_sha256=canonical_json_sha256(document))


def validate_branch_set_plan(
    plan: BranchSetPlan,
    selections: SelectionDocuments,
) -> None:
    parent_roots = set(plan.parent_work.inputs)
    for branch in plan.branches:
        selection = resolve_selection(branch.artifact_selection, selections)
        if branch.workflow_plan.work.inputs != selection.roots():
            raise ValueError("branch work roots differ from its artifact selection")
        if not set(selection.roots()).issubset(parent_roots):
            raise ValueError("branch selection contains artifacts outside the parent work roots")
        binding = branch.workflow_plan.work.fork_join
        if not isinstance(binding, BranchWorkBinding):
            raise ValueError("branch work is missing its branch binding")
        if (
            binding.parent_work_id != plan.parent_work.work_id
            or binding.branch_id != branch.branch_id
            or binding.decision_sha256 != plan.decision_sha256
            or binding.artifact_selection_sha256 != selection.selection_sha256
        ):
            raise ValueError("branch work binding differs from the branch-set declaration")


def _branch_plans(plan: BranchSetPlan) -> dict[str, BranchPlan]:
    return {item.branch_id: item for item in plan.branches}


def _normalize_branch_results(
    plan: BranchSetPlan,
    selections: SelectionDocuments,
    settlements: Sequence[BranchSettlement],
    outcomes: Sequence[BranchOutcome],
) -> tuple[dict[str, BranchSettlement], dict[str, BranchOutcome]]:
    branches = _branch_plans(plan)
    settlement_map: dict[str, BranchSettlement] = {}
    outcome_map: dict[str, BranchOutcome] = {}
    roots: set[CollectionRootRef] = set()
    for settlement in settlements:
        branch = branches.get(settlement.branch_id)
        if branch is None:
            raise ValueError(f"settlement references unknown branch: {settlement.branch_id}")
        if settlement.branch_id in settlement_map:
            raise ValueError(f"duplicate settlement for branch: {settlement.branch_id}")
        if (
            settlement.work_id != branch.workflow_plan.work.work_id
            or settlement.workflow_plan_sha256 != branch.workflow_plan.workflow_plan_sha256
        ):
            raise ValueError("branch settlement does not bind the declared workflow plan")
        selection = resolve_selection(settlement.output_selection, selections)
        if any(item.collection != settlement.output_collection for item in selection.artifacts):
            raise ValueError("branch output selection differs from its output collection")
        if settlement.output_collection in roots:
            raise ValueError("two branches cannot settle to the same output collection root")
        roots.add(settlement.output_collection)
        settlement_map[settlement.branch_id] = settlement
    for outcome in outcomes:
        branch = branches.get(outcome.branch_id)
        if branch is None:
            raise ValueError(f"outcome references unknown branch: {outcome.branch_id}")
        if outcome.branch_id in outcome_map:
            raise ValueError(f"duplicate outcome for branch: {outcome.branch_id}")
        if outcome.branch_id in settlement_map:
            raise ValueError("a branch cannot have both success settlement and terminal outcome")
        if (
            outcome.work_id != branch.workflow_plan.work.work_id
            or outcome.workflow_plan_sha256 != branch.workflow_plan.workflow_plan_sha256
        ):
            raise ValueError("branch outcome does not bind the declared workflow plan")
        outcome_map[outcome.branch_id] = outcome
    return settlement_map, outcome_map


def _join_member_selection(
    declaration: JoinMemberDeclaration,
    settlement: BranchSettlement,
    selections: SelectionDocuments,
) -> ArtifactSelection:
    output = resolve_selection(settlement.output_selection, selections)
    allowed = set(declaration.output_roles)
    selected = tuple(item for item in output.artifacts if item.role in allowed)
    present = {item.role for item in selected}
    missing = sorted(allowed - present)
    if missing:
        raise ValueError(
            f"branch {declaration.branch_id} output lacks declared join role(s): "
            + ", ".join(missing)
        )
    return ArtifactSelection.seal(selected)


def resolve_join_plan(
    plan: BranchSetPlan,
    selections: SelectionDocuments,
    settlements: Sequence[BranchSettlement],
) -> tuple[JoinPlan, tuple[ArtifactSelection, ...]] | None:
    """Resolve the exact join only when every named member settled successfully."""

    validate_branch_set_plan(plan, selections)
    if plan.join is None:
        return None
    settlement_map, _ = _normalize_branch_results(plan, selections, settlements, ())
    if any(item.branch_id not in settlement_map for item in plan.join.members):
        return None
    resolved_selections: list[ArtifactSelection] = []
    inputs: list[JoinInputPlan] = []
    member_bindings: list[JoinWorkMemberBinding] = []
    for member in plan.join.members:
        settlement = settlement_map[member.branch_id]
        selection = _join_member_selection(member, settlement, selections)
        resolved_selections.append(selection)
        inputs.append(
            JoinInputPlan(
                branch_id=member.branch_id,
                settlement_sha256=settlement.settlement_sha256,
                derivation_sha256=settlement.derivation_sha256,
                output_collection=settlement.output_collection,
                artifact_selection=selection.ref(),
            )
        )
        member_bindings.append(
            JoinWorkMemberBinding(
                branch_id=member.branch_id,
                settlement_sha256=settlement.settlement_sha256,
                artifact_selection_sha256=selection.selection_sha256,
            )
        )
    roots = tuple(sorted({item.output_collection for item in inputs}, key=_root_key))
    work = WorkIdentity.seal(
        WorkPayload(
            recipe=plan.join.recipe,
            inputs=roots,
            effective_intent=plan.join.effective_intent,
            fork_join=JoinWorkBinding(
                parent_work_id=plan.parent_work.work_id,
                branch_set_sha256=plan.branch_set_sha256,
                members=tuple(member_bindings),
            ),
        )
    )
    workflow_plan = plan.join.workflow_intent.materialize(work=work)
    join_plan = JoinPlan.seal(
        parent_work_id=plan.parent_work.work_id,
        branch_set_sha256=plan.branch_set_sha256,
        declaration=plan.join,
        inputs=inputs,
        work=work,
        workflow_plan=workflow_plan,
    )
    return join_plan, tuple(resolved_selections)


def _validate_join_result(
    join_plan: JoinPlan,
    selections: SelectionDocuments,
    branch_settlements: Mapping[str, BranchSettlement],
    settlement: JoinSettlement | None,
    outcome: JoinOutcome | None,
) -> None:
    if settlement is not None and outcome is not None:
        raise ValueError("join cannot have both success settlement and terminal outcome")
    expected = (
        join_plan.work.work_id,
        join_plan.workflow_plan.workflow_plan_sha256,
        join_plan.join_plan_sha256,
    )
    if settlement is not None:
        if (
            settlement.work_id,
            settlement.workflow_plan_sha256,
            settlement.join_plan_sha256,
        ) != expected:
            raise ValueError("join settlement does not bind the resolved join plan")
        selection = resolve_selection(settlement.output_selection, selections)
        if any(item.collection != settlement.output_collection for item in selection.artifacts):
            raise ValueError("join output selection differs from its output collection")
        branch_roots = {item.output_collection for item in branch_settlements.values()}
        if settlement.output_collection in branch_roots:
            raise ValueError("join output must be an additional retained collection")
    if (
        outcome is not None
        and (
            outcome.work_id,
            outcome.workflow_plan_sha256,
            outcome.join_plan_sha256,
        )
        != expected
    ):
        raise ValueError("join outcome does not bind the resolved join plan")


def evaluate_branch_set(
    plan: BranchSetPlan,
    selections: SelectionDocuments,
    *,
    branch_settlements: Sequence[BranchSettlement] = (),
    branch_outcomes: Sequence[BranchOutcome] = (),
    join_settlement: JoinSettlement | None = None,
    join_outcome: JoinOutcome | None = None,
) -> BranchSetEvaluation:
    """Compute the complete fork/join view without creating mutable graph state."""

    validate_branch_set_plan(plan, selections)
    settlements, outcomes = _normalize_branch_results(
        plan,
        selections,
        branch_settlements,
        branch_outcomes,
    )
    branch_ids = tuple(item.branch_id for item in plan.branches)
    unsettled = tuple(
        item
        for item in branch_ids
        if item not in settlements
        and (item not in outcomes or outcomes[item].state == "interrupted")
    )
    failed = tuple(
        item for item in branch_ids if outcomes.get(item) and outcomes[item].state == "failed"
    )
    inapplicable = tuple(
        item for item in branch_ids if outcomes.get(item) and outcomes[item].state == "inapplicable"
    )
    interrupted = tuple(
        item for item in branch_ids if outcomes.get(item) and outcomes[item].state == "interrupted"
    )
    canceled = tuple(
        item for item in branch_ids if outcomes.get(item) and outcomes[item].state == "canceled"
    )

    resolved_join: JoinPlan | None = None
    join_ready = False
    join_state: JoinEvaluationState = "not-declared"
    if plan.join is None:
        if join_settlement is not None or join_outcome is not None:
            raise ValueError("branch set has no join declaration")
    else:
        resolution = resolve_join_plan(plan, selections, tuple(settlements.values()))
        if resolution is None:
            if join_settlement is not None or join_outcome is not None:
                raise ValueError("join result exists before every named member succeeded")
            join_state = "waiting"
        else:
            resolved_join, _resolved_selections = resolution
            join_ready = True
            _validate_join_result(
                resolved_join,
                selections,
                settlements,
                join_settlement,
                join_outcome,
            )
            if join_settlement is not None:
                join_state = "succeeded"
            elif join_outcome is not None:
                join_state = join_outcome.state
            else:
                join_state = "ready"

    all_branches_succeeded = len(settlements) == len(plan.branches)
    join_succeeded = plan.join is None or join_settlement is not None
    succeeded = all_branches_succeeded and join_succeeded
    unsettled_work_ids = [
        _branch_plans(plan)[branch_id].workflow_plan.work.work_id for branch_id in unsettled
    ]
    if (
        resolved_join is not None
        and join_settlement is None
        and (join_outcome is None or join_outcome.state == "interrupted")
    ):
        unsettled_work_ids.append(resolved_join.work.work_id)

    return BranchSetEvaluation(
        branch_set_sha256=plan.branch_set_sha256,
        succeeded_branches=tuple(settlements[item] for item in sorted(settlements)),
        unsettled_branch_ids=unsettled,
        failed_branch_ids=failed,
        inapplicable_branch_ids=inapplicable,
        interrupted_branch_ids=interrupted,
        canceled_branch_ids=canceled,
        join_ready=join_ready,
        resolved_join_plan=resolved_join,
        join_state=join_state,
        join_settlement=join_settlement,
        unsettled_work_ids=tuple(sorted(unsettled_work_ids)),
        branch_set_succeeded=succeeded,
        retirement_requested=plan.retirement_policy == "retire-after-verified-output",
        coordination_complete_for_retirement=succeeded,
    )


__all__ = [
    "ARTIFACT_SELECTION_FORMAT",
    "BRANCH_OUTCOME_FORMAT",
    "BRANCH_SET_FORMAT",
    "BRANCH_SETTLEMENT_FORMAT",
    "JOIN_DECLARATION_FORMAT",
    "JOIN_OUTCOME_FORMAT",
    "JOIN_PLAN_FORMAT",
    "JOIN_SETTLEMENT_FORMAT",
    "ArtifactSelection",
    "ArtifactSelectionRef",
    "BranchOutcome",
    "BranchPlan",
    "BranchSetDecision",
    "BranchSetEvaluation",
    "BranchSetPlan",
    "BranchSettlement",
    "BranchTargetPreview",
    "BranchOutcomeState",
    "JoinDeclaration",
    "JoinEvaluationState",
    "JoinInputPlan",
    "JoinMemberDeclaration",
    "JoinOutcome",
    "JoinPlan",
    "JoinSettlement",
    "JoinOutcomeState",
    "SelectionDocuments",
    "WorkflowPreview",
    "WorkflowPreviewPayload",
    "evaluate_branch_set",
    "resolve_join_plan",
    "resolve_selection",
    "validate_branch_set_plan",
]

"""Derived fork/join truth over ordinary durable Stove0 work records."""

from __future__ import annotations

from dataclasses import dataclass

from stove0_protocol import (
    ArtifactSelection,
    ArtifactSubject,
    BranchOutcome,
    BranchOutcomeState,
    BranchPlan,
    BranchSetEvaluation,
    BranchSettlement,
    CollectionRootRef,
    JoinOutcome,
    JoinOutcomeState,
    JoinPlan,
    JoinSettlement,
    evaluate_branch_set,
    resolve_join_plan,
)
from stove0_target_protocol import OutputCollectionRef

from stove0_core.work_state import WorkRecord, WorkStore


@dataclass(frozen=True, slots=True)
class CoordinationProjection:
    """Current semantic graph view plus an optional join admission."""

    evaluation: BranchSetEvaluation
    selection_documents: dict[str, ArtifactSelection]
    pending_join: JoinPlan | None = None
    pending_join_selections: tuple[ArtifactSelection, ...] = ()


def project_coordination(parent: WorkRecord, store: WorkStore) -> CoordinationProjection:
    """Project one branch set without persisting another workflow state machine."""

    plan = parent.branch_set_plan
    if plan is None or parent.work != plan.parent_work:
        raise ValueError("coordination parent has no matching branch-set plan")

    selections = _declared_selections(parent, store)
    settlements: list[BranchSettlement] = []
    outcomes: list[BranchOutcome] = []
    for branch in plan.branches:
        child = _load_declared_work(store, branch.workflow_plan.work.work_id)
        if child.workflow_plan != branch.workflow_plan:
            raise RuntimeError("durable branch child differs from its sealed workflow plan")
        settlement = _branch_settlement(child)
        if settlement is not None:
            output_selection = _output_selection(child)
            _retain_selection(selections, output_selection)
            settlements.append(
                BranchSettlement.seal(
                    branch=branch,
                    derivation_sha256=settlement.derivation_sha256,
                    output_collection=_collection_root(settlement),
                    output_selection=output_selection,
                )
            )
            continue
        outcome = _branch_outcome(branch, child)
        if outcome is not None:
            outcomes.append(outcome)

    resolution = resolve_join_plan(plan, selections, settlements)
    pending_join: JoinPlan | None = None
    pending_join_selections: tuple[ArtifactSelection, ...] = ()
    join_settlement: JoinSettlement | None = None
    join_outcome: JoinOutcome | None = None
    if parent.join_plan is not None:
        if resolution is None or parent.join_plan != resolution[0]:
            raise RuntimeError("durable join plan differs from exact branch settlements")
        join_plan, join_selections = resolution
        for selection in join_selections:
            _retain_selection(selections, selection)
        join_record = _load_declared_work(store, join_plan.work.work_id)
        if join_record.workflow_plan != join_plan.workflow_plan:
            raise RuntimeError("durable join child differs from its sealed workflow plan")
        join_settlement = _join_settlement(join_plan, join_record, selections)
        if join_settlement is None:
            join_outcome = _join_outcome(join_plan, join_record)
    elif resolution is not None:
        pending_join, pending_join_selections = resolution
        for selection in pending_join_selections:
            _retain_selection(selections, selection)

    evaluation = evaluate_branch_set(
        plan,
        selections,
        branch_settlements=settlements,
        branch_outcomes=outcomes,
        join_settlement=join_settlement,
        join_outcome=join_outcome,
    )
    return CoordinationProjection(
        evaluation=evaluation,
        selection_documents=selections,
        pending_join=pending_join,
        pending_join_selections=pending_join_selections,
    )


def _declared_selections(
    parent: WorkRecord,
    store: WorkStore,
) -> dict[str, ArtifactSelection]:
    assert parent.branch_set_plan is not None
    documents: dict[str, ArtifactSelection] = {}
    for branch in parent.branch_set_plan.branches:
        digest = branch.artifact_selection.selection_sha256
        selection = store.load_selection(digest)
        if selection is None:
            raise RuntimeError(f"durable branch selection is unavailable: {digest}")
        _retain_selection(documents, selection)
    return documents


def _load_declared_work(store: WorkStore, work_id: str) -> WorkRecord:
    record = store.load(work_id)
    if record is None:
        raise RuntimeError(f"atomically admitted child work is unavailable: {work_id}")
    return record


def _branch_settlement(record: WorkRecord) -> OutputCollectionRef | None:
    if record.phase not in {"settled", "retirement_pending", "complete"}:
        return None
    output = record.output
    if output is None:
        raise RuntimeError("verified branch work has no exact output collection")
    return output


def _branch_outcome(branch: BranchPlan, record: WorkRecord) -> BranchOutcome | None:
    state: BranchOutcomeState | None = None
    if record.phase == "failed":
        state = "failed"
    elif record.phase == "inapplicable":
        state = "inapplicable"
    elif record.phase == "canceled":
        state = "canceled"
    elif record.target_status is not None and record.target_status.state == "interrupted":
        state = "interrupted"
    if state is None:
        return None
    return BranchOutcome(
        branch_id=branch.branch_id,
        work_id=branch.workflow_plan.work.work_id,
        workflow_plan_sha256=branch.workflow_plan.workflow_plan_sha256,
        state=state,
    )


def _join_settlement(
    plan: JoinPlan,
    record: WorkRecord,
    selections: dict[str, ArtifactSelection],
) -> JoinSettlement | None:
    if record.phase not in {"settled", "retirement_pending", "complete"}:
        return None
    output = record.output
    if output is None:
        raise RuntimeError("verified join work has no exact output collection")
    selection = _output_selection(record)
    _retain_selection(selections, selection)
    return JoinSettlement.seal(
        plan=plan,
        derivation_sha256=output.derivation_sha256,
        output_collection=_collection_root(output),
        output_selection=selection,
    )


def _join_outcome(plan: JoinPlan, record: WorkRecord) -> JoinOutcome | None:
    state: JoinOutcomeState | None = None
    if record.phase == "failed":
        state = "failed"
    elif record.phase == "inapplicable":
        state = "inapplicable"
    elif record.phase == "canceled":
        state = "canceled"
    elif record.target_status is not None and record.target_status.state == "interrupted":
        state = "interrupted"
    if state is None:
        return None
    return JoinOutcome(
        work_id=plan.work.work_id,
        workflow_plan_sha256=plan.workflow_plan.workflow_plan_sha256,
        join_plan_sha256=plan.join_plan_sha256,
        state=state,
    )


def _output_selection(record: WorkRecord) -> ArtifactSelection:
    output = record.output
    status = record.target_status
    if (
        output is None
        or status is None
        or status.state != "succeeded"
        or status.output_collection != output
    ):
        raise RuntimeError("settled work lacks matching immutable target output evidence")
    root = _collection_root(output)
    return ArtifactSelection.seal(
        tuple(
            ArtifactSubject(
                id=item.id,
                role=item.role,
                collection=root,
                path=item.path,
                bytes=item.bytes,
                sha256=item.sha256,
                media_type=item.media_type,
            )
            for item in status.outputs
        )
    )


def _collection_root(output: OutputCollectionRef) -> CollectionRootRef:
    return CollectionRootRef(
        collection_id=output.collection_id,
        manifest_sha256=output.manifest_sha256,
        content_etag=output.content_etag,
    )


def _retain_selection(
    documents: dict[str, ArtifactSelection],
    selection: ArtifactSelection,
) -> None:
    existing = documents.setdefault(selection.selection_sha256, selection)
    if existing != selection:
        raise RuntimeError("artifact selection identity was reused")


__all__ = ["CoordinationProjection", "project_coordination"]

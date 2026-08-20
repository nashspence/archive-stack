from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError
from stove0_protocol import (
    ArtifactSelection,
    ArtifactSelectionRef,
    ArtifactSubject,
    BranchOutcome,
    BranchOutcomeState,
    BranchPlan,
    BranchSetPlan,
    BranchSettlement,
    CollectionRootRef,
    EvaluationBinding,
    JoinDeclaration,
    JoinMemberDeclaration,
    JoinOutcome,
    JoinSettlement,
    OperationRef,
    RecipeRef,
    RetirementPolicy,
    WorkflowPlanIntent,
    WorkIdentity,
    WorkPayload,
    canonical_json_bytes,
    evaluate_branch_set,
    resolve_join_plan,
)


def digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def root(number: int, label: str | None = None) -> CollectionRootRef:
    suffix = label or str(number)
    return CollectionRootRef(
        collection_id=number,
        manifest_sha256=digest(f"manifest:{suffix}"),
        content_etag=digest(f"content:{suffix}"),
    )


def artifact(
    artifact_id: str,
    collection: CollectionRootRef,
    path: str,
    *,
    role: str = "source.primary/v1",
    byte_count: int = 10,
) -> ArtifactSubject:
    return ArtifactSubject(
        id=artifact_id,
        role=role,
        collection=collection,
        path=path,
        bytes=byte_count,
        sha256=digest(f"artifact:{collection.collection_id}:{path}:{byte_count}"),
    )


def recipe(label: str = "parent") -> RecipeRef:
    return RecipeRef(id=f"recipe.{label}/v1", revision=1, sha256=digest(f"recipe:{label}"))


def workflow_intent(
    label: str,
    *,
    option: int = 1,
    retirement: RetirementPolicy = "retain",
) -> WorkflowPlanIntent:
    return WorkflowPlanIntent(
        operation=OperationRef(
            id=f"operation.{label}/v1",
            sha256=digest(f"operation:{label}"),
        ),
        target_registration_id=f"target-{label}",
        target_contract_sha256=digest(f"target-contract:{label}"),
        requested_target_options={"option": option},
        input_retrieval_policy="available-only",
        output_tags=(f"output-{label}",),
        retirement_policy=retirement,
        output_policy={"kind": label},
    )


def parent_work(*roots: CollectionRootRef, evaluation: bool = False) -> WorkIdentity:
    ordered = tuple(sorted(roots, key=lambda item: item.collection_id))
    return WorkIdentity.seal(
        WorkPayload(
            recipe=recipe(),
            inputs=ordered,
            effective_intent={"mode": "fork"},
            evaluation=(
                EvaluationBinding(
                    evaluation_id=digest("evaluation"),
                    matrix_sha256=digest("matrix"),
                    variant_id="base",
                )
                if evaluation
                else None
            ),
        )
    )


def branch(
    *,
    parent: WorkIdentity,
    branch_id: str,
    decision: str,
    selection: ArtifactSelection,
    option: int = 1,
) -> BranchPlan:
    return BranchPlan.build(
        parent_work=parent,
        branch_id=branch_id,
        decision_sha256=decision,
        selection=selection,
        recipe=recipe(branch_id),
        effective_intent={"branch": branch_id},
        workflow_intent=workflow_intent(branch_id, option=option),
    )


def branch_set_fixture(
    *,
    with_join: bool = True,
    evaluation: bool = False,
) -> tuple[BranchSetPlan, dict[str, ArtifactSelection], dict[str, BranchPlan]]:
    source_a = root(1)
    source_b = root(2)
    parent = parent_work(source_a, source_b, evaluation=evaluation)
    decision = digest("decision")
    video_selection = ArtifactSelection.seal(
        (
            artifact("shared", source_a, "source/shared.mov"),
            artifact("video-extra", source_b, "source/video-extra.json"),
        )
    )
    audio_selection = ArtifactSelection.seal(
        (
            artifact("audio", source_b, "source/audio.wav"),
            artifact("shared", source_a, "source/shared.mov"),
        )
    )
    metadata_selection = ArtifactSelection.seal(
        (artifact("metadata", source_a, "source/metadata.xmp"),)
    )
    plans = {
        "audio": branch(
            parent=parent,
            branch_id="audio",
            decision=decision,
            selection=audio_selection,
        ),
        "metadata": branch(
            parent=parent,
            branch_id="metadata",
            decision=decision,
            selection=metadata_selection,
        ),
        "video": branch(
            parent=parent,
            branch_id="video",
            decision=decision,
            selection=video_selection,
        ),
    }
    selections = {
        item.selection_sha256: item
        for item in (video_selection, audio_selection, metadata_selection)
    }
    join = (
        JoinDeclaration.seal(
            members=(
                JoinMemberDeclaration(
                    branch_id="audio",
                    output_roles=("archive.audio/v1",),
                ),
                JoinMemberDeclaration(
                    branch_id="video",
                    output_roles=("archive.video/v1",),
                ),
            ),
            recipe=recipe("join"),
            effective_intent={"container": "matroska"},
            workflow_intent=workflow_intent("join"),
        )
        if with_join
        else None
    )
    plan = BranchSetPlan.seal(
        parent_work=parent,
        decision_sha256=decision,
        evidence_sha256s=(digest("observation"), digest("review")),
        branches=tuple(reversed(tuple(plans.values()))),
        join=join,
        retirement_policy="retain" if evaluation else "retire-after-verified-output",
        selections=selections,
    )
    return plan, selections, plans


def successful_branch(
    plan: BranchPlan,
    label: str,
    role: str,
) -> tuple[BranchSettlement, ArtifactSelection]:
    output = root(100 + sum(label.encode()), f"output:{label}")
    selection = ArtifactSelection.seal(
        (artifact(f"{label}-output", output, f"{label}/output.bin", role=role),)
    )
    return (
        BranchSettlement.seal(
            branch=plan,
            derivation_sha256=digest(f"derivation:{label}"),
            output_collection=output,
            output_selection=selection,
        ),
        selection,
    )


def settled_fixture() -> tuple[
    BranchSetPlan,
    dict[str, ArtifactSelection],
    dict[str, BranchSettlement],
]:
    plan, selections, branches = branch_set_fixture()
    audio, audio_selection = successful_branch(branches["audio"], "audio", "archive.audio/v1")
    metadata, metadata_selection = successful_branch(
        branches["metadata"], "metadata", "archive.metadata/v1"
    )
    video, video_selection = successful_branch(branches["video"], "video", "archive.video/v1")
    selections.update(
        {
            audio_selection.selection_sha256: audio_selection,
            metadata_selection.selection_sha256: metadata_selection,
            video_selection.selection_sha256: video_selection,
        }
    )
    return plan, selections, {"audio": audio, "metadata": metadata, "video": video}


def test_one_branch_and_many_branches_use_the_same_contract() -> None:
    plan, selections, branches = branch_set_fixture(with_join=False)
    single = BranchSetPlan.seal(
        parent_work=plan.parent_work,
        decision_sha256=plan.decision_sha256,
        branches=(branches["video"],),
        selections=selections,
    )
    assert isinstance(single, BranchSetPlan)
    assert len(single.branches) == 1
    assert isinstance(plan, BranchSetPlan)
    assert len(plan.branches) == 3


def test_parent_work_may_contain_multiple_exact_collection_roots() -> None:
    plan, _, _ = branch_set_fixture()
    assert len(plan.parent_work.inputs) == 2


def test_branch_selections_may_overlap_without_partition_semantics() -> None:
    _, selections, branches = branch_set_fixture()
    audio = selections[branches["audio"].artifact_selection.selection_sha256]
    video = selections[branches["video"].artifact_selection.selection_sha256]
    audio_paths = {(item.collection.collection_id, item.path) for item in audio.artifacts}
    video_paths = {(item.collection.collection_id, item.path) for item in video.artifacts}
    assert audio_paths & video_paths == {(1, "source/shared.mov")}


def test_child_work_inputs_are_exactly_selection_roots() -> None:
    _, selections, branches = branch_set_fixture()
    for branch_plan in branches.values():
        selection = selections[branch_plan.artifact_selection.selection_sha256]
        assert branch_plan.workflow_plan.work.inputs == selection.roots()
        assert branch_plan.workflow_plan.work.fork_join is not None
        assert "branch_set" not in branch_plan.workflow_plan.work.model_dump_json()


def test_branch_set_rejects_nested_parent_work() -> None:
    plan, selections, branches = branch_set_fixture(with_join=False)
    nested_parent = branches["video"].workflow_plan.work
    nested_branch = branch(
        parent=nested_parent,
        branch_id="nested",
        decision=digest("nested-decision"),
        selection=selections[branches["video"].artifact_selection.selection_sha256],
    )
    with pytest.raises(ValidationError, match="top-level"):
        BranchSetPlan.seal(
            parent_work=nested_parent,
            decision_sha256=digest("nested-decision"),
            branches=(nested_branch,),
            selections=selections,
        )


def test_branch_set_identity_and_bytes_ignore_declaration_order() -> None:
    plan, selections, branches = branch_set_fixture()
    rebuilt = BranchSetPlan.seal(
        parent_work=plan.parent_work,
        decision_sha256=plan.decision_sha256,
        evidence_sha256s=tuple(reversed(plan.evidence_sha256s)),
        branches=(branches["metadata"], branches["video"], branches["audio"]),
        join=plan.join,
        retirement_policy=plan.retirement_policy,
        selections=selections,
    )
    assert rebuilt.branch_set_sha256 == plan.branch_set_sha256
    assert rebuilt.canonical_bytes() == plan.canonical_bytes()


def test_semantic_declaration_changes_change_branch_set_identity() -> None:
    plan, selections, branches = branch_set_fixture()
    changed_branch = branch(
        parent=plan.parent_work,
        branch_id="video",
        decision=plan.decision_sha256,
        selection=selections[branches["video"].artifact_selection.selection_sha256],
        option=2,
    )
    changed = BranchSetPlan.seal(
        parent_work=plan.parent_work,
        decision_sha256=plan.decision_sha256,
        evidence_sha256s=plan.evidence_sha256s,
        branches=(branches["audio"], branches["metadata"], changed_branch),
        join=plan.join,
        retirement_policy=plan.retirement_policy,
        selections=selections,
    )
    assert changed.branch_set_sha256 != plan.branch_set_sha256


def test_branch_set_identity_changes_for_decision_evidence_selection_and_membership() -> None:
    plan, selections, branches = branch_set_fixture(with_join=False)
    video_selection = selections[branches["video"].artifact_selection.selection_sha256]

    changed_decision_value = digest("other-decision")
    changed_decision_branches = tuple(
        branch(
            parent=plan.parent_work,
            branch_id=branch_id,
            decision=changed_decision_value,
            selection=selections[current.artifact_selection.selection_sha256],
        )
        for branch_id, current in sorted(branches.items())
    )
    changed_decision = BranchSetPlan.seal(
        parent_work=plan.parent_work,
        decision_sha256=changed_decision_value,
        branches=changed_decision_branches,
        selections=selections,
    )
    assert changed_decision.branch_set_sha256 != plan.branch_set_sha256

    changed_evidence = BranchSetPlan.seal(
        parent_work=plan.parent_work,
        decision_sha256=plan.decision_sha256,
        evidence_sha256s=(digest("other-evidence"),),
        branches=plan.branches,
        selections=selections,
    )
    assert changed_evidence.branch_set_sha256 != plan.branch_set_sha256

    smaller_selection = ArtifactSelection.seal((video_selection.artifacts[0],))
    selections[smaller_selection.selection_sha256] = smaller_selection
    changed_selection_branch = branch(
        parent=plan.parent_work,
        branch_id="video",
        decision=plan.decision_sha256,
        selection=smaller_selection,
    )
    changed_selection = BranchSetPlan.seal(
        parent_work=plan.parent_work,
        decision_sha256=plan.decision_sha256,
        branches=(branches["audio"], branches["metadata"], changed_selection_branch),
        selections=selections,
    )
    assert changed_selection.branch_set_sha256 != plan.branch_set_sha256
    assert (
        changed_selection_branch.workflow_plan.work.work_id
        != branches["video"].workflow_plan.work.work_id
    )

    changed_membership = BranchSetPlan.seal(
        parent_work=plan.parent_work,
        decision_sha256=plan.decision_sha256,
        branches=(branches["audio"], branches["video"]),
        selections=selections,
    )
    assert changed_membership.branch_set_sha256 != plan.branch_set_sha256


def test_selection_reference_is_closed_and_summary_verified() -> None:
    selection = ArtifactSelection.seal((artifact("a", root(1), "a"),))
    # A reference is intentionally useful without the document; the resolver is
    # the authority that verifies its required redundant summary.
    bad_ref = ArtifactSelectionRef(
        selection_sha256=selection.selection_sha256,
        artifact_count=2,
        total_bytes=selection.total_bytes,
    )
    from stove0_protocol.fork_join import resolve_selection

    with pytest.raises(ValueError, match="summary"):
        resolve_selection(bad_ref, {selection.selection_sha256: selection})


def test_join_is_not_resolved_until_every_named_branch_succeeds() -> None:
    plan, selections, settlements = settled_fixture()
    assert resolve_join_plan(plan, selections, (settlements["audio"],)) is None
    resolution = resolve_join_plan(
        plan,
        selections,
        (settlements["video"], settlements["audio"]),
    )
    assert resolution is not None


def test_named_subset_join_becomes_ready_while_other_branch_is_unsettled() -> None:
    plan, selections, settlements = settled_fixture()
    evaluation = evaluate_branch_set(
        plan,
        selections,
        branch_settlements=(settlements["video"], settlements["audio"]),
    )
    assert evaluation.join_ready is True
    assert evaluation.join_state == "ready"
    assert evaluation.unsettled_branch_ids == ("metadata",)
    assert evaluation.branch_set_succeeded is False


def test_join_identity_is_independent_of_branch_result_arrival_order() -> None:
    plan, selections, settlements = settled_fixture()
    first = resolve_join_plan(
        plan,
        selections,
        (settlements["audio"], settlements["video"]),
    )
    second = resolve_join_plan(
        plan,
        selections,
        (settlements["video"], settlements["audio"]),
    )
    assert first is not None and second is not None
    assert first[0].join_plan_sha256 == second[0].join_plan_sha256
    assert first[0].work.work_id == second[0].work.work_id


def test_resolved_join_binds_exact_collections_and_role_filtered_selections() -> None:
    plan, selections, settlements = settled_fixture()
    resolution = resolve_join_plan(plan, selections, tuple(settlements.values()))
    assert resolution is not None
    join_plan, join_selections = resolution
    assert tuple(item.branch_id for item in join_plan.inputs) == ("audio", "video")
    assert {item.output_collection for item in join_plan.inputs} == {
        settlements["audio"].output_collection,
        settlements["video"].output_collection,
    }
    assert {item.artifacts[0].role for item in join_selections} == {
        "archive.audio/v1",
        "archive.video/v1",
    }
    assert join_plan.work.fork_join is not None
    assert "join_plan_sha256" not in join_plan.work.model_dump_json()


def test_changed_branch_result_changes_resolved_join_identity() -> None:
    plan, selections, settlements = settled_fixture()
    first = resolve_join_plan(plan, selections, (settlements["audio"], settlements["video"]))
    assert first is not None

    video_branch = next(item for item in plan.branches if item.branch_id == "video")
    replacement, replacement_selection = successful_branch(
        video_branch, "video-replacement", "archive.video/v1"
    )
    selections[replacement_selection.selection_sha256] = replacement_selection
    second = resolve_join_plan(plan, selections, (settlements["audio"], replacement))
    assert second is not None
    assert second[0].work.work_id != first[0].work.work_id
    assert second[0].join_plan_sha256 != first[0].join_plan_sha256


def test_changed_branch_settlement_changes_resolved_join_identity() -> None:
    plan, selections, settlements = settled_fixture()
    first = resolve_join_plan(plan, selections, (settlements["audio"], settlements["video"]))
    assert first is not None
    video_branch = next(item for item in plan.branches if item.branch_id == "video")
    video_selection = selections[settlements["video"].output_selection.selection_sha256]
    replacement = BranchSettlement.seal(
        branch=video_branch,
        derivation_sha256=digest("replacement-derivation"),
        output_collection=settlements["video"].output_collection,
        output_selection=video_selection,
    )
    second = resolve_join_plan(plan, selections, (settlements["audio"], replacement))
    assert second is not None
    assert second[0].work.work_id != first[0].work.work_id
    assert second[0].join_plan_sha256 != first[0].join_plan_sha256


def test_evaluation_is_independent_of_result_arrival_order() -> None:
    plan, selections, settlements = settled_fixture()
    first = evaluate_branch_set(
        plan,
        selections,
        branch_settlements=(
            settlements["audio"],
            settlements["metadata"],
            settlements["video"],
        ),
    )
    second = evaluate_branch_set(
        plan,
        selections,
        branch_settlements=(
            settlements["video"],
            settlements["audio"],
            settlements["metadata"],
        ),
    )
    assert first == second


def test_join_settlement_is_additional_and_branch_outputs_remain_visible() -> None:
    plan, selections, settlements = settled_fixture()
    resolution = resolve_join_plan(plan, selections, tuple(settlements.values()))
    assert resolution is not None
    join_plan, join_inputs = resolution
    selections.update({item.selection_sha256: item for item in join_inputs})
    join_root = root(900, "joined")
    join_output = ArtifactSelection.seal(
        (artifact("joined", join_root, "joined/output.mkv", role="archive.joined/v1"),)
    )
    selections[join_output.selection_sha256] = join_output
    join_settlement = JoinSettlement.seal(
        plan=join_plan,
        derivation_sha256=digest("join-derivation"),
        output_collection=join_root,
        output_selection=join_output,
    )
    evaluation = evaluate_branch_set(
        plan,
        selections,
        branch_settlements=tuple(settlements.values()),
        join_settlement=join_settlement,
    )
    assert evaluation.branch_set_succeeded is True
    assert len(evaluation.succeeded_branches) == 3
    assert join_settlement.output_collection not in {
        item.output_collection for item in evaluation.succeeded_branches
    }


def test_failed_nonmember_preserves_join_but_prevents_aggregate_success() -> None:
    plan, selections, settlements = settled_fixture()
    resolution = resolve_join_plan(
        plan,
        selections,
        (settlements["audio"], settlements["video"]),
    )
    assert resolution is not None
    join_plan, join_inputs = resolution
    selections.update({item.selection_sha256: item for item in join_inputs})
    join_root = root(901, "joined-partial")
    join_output = ArtifactSelection.seal(
        (artifact("joined", join_root, "joined/output.mkv", role="archive.joined/v1"),)
    )
    selections[join_output.selection_sha256] = join_output
    join_settlement = JoinSettlement.seal(
        plan=join_plan,
        derivation_sha256=digest("join-partial"),
        output_collection=join_root,
        output_selection=join_output,
    )
    metadata = _branch_outcome(plan, "metadata", "failed")
    evaluation = evaluate_branch_set(
        plan,
        selections,
        branch_settlements=(settlements["audio"], settlements["video"]),
        branch_outcomes=(metadata,),
        join_settlement=join_settlement,
    )
    assert evaluation.join_state == "succeeded"
    assert evaluation.failed_branch_ids == ("metadata",)
    assert evaluation.branch_set_succeeded is False
    assert evaluation.coordination_complete_for_retirement is False


def _branch_outcome(
    plan: BranchSetPlan,
    branch_id: str,
    state: BranchOutcomeState,
) -> BranchOutcome:
    branch_plan = next(item for item in plan.branches if item.branch_id == branch_id)
    return BranchOutcome(
        branch_id=branch_id,
        work_id=branch_plan.workflow_plan.work.work_id,
        workflow_plan_sha256=branch_plan.workflow_plan.workflow_plan_sha256,
        state=state,
    )


def test_failed_branch_preserves_successful_sibling_and_blocks_join() -> None:
    plan, selections, settlements = settled_fixture()
    evaluation = evaluate_branch_set(
        plan,
        selections,
        branch_settlements=(settlements["video"],),
        branch_outcomes=(_branch_outcome(plan, "audio", "failed"),),
    )
    assert tuple(item.branch_id for item in evaluation.succeeded_branches) == ("video",)
    assert evaluation.failed_branch_ids == ("audio",)
    assert evaluation.join_ready is False
    assert evaluation.branch_set_succeeded is False


def test_interrupted_branch_remains_unsettled_and_resumable() -> None:
    plan, selections, _ = settled_fixture()
    outcome = _branch_outcome(plan, "audio", "interrupted")
    evaluation = evaluate_branch_set(
        plan,
        selections,
        branch_outcomes=(outcome,),
    )
    assert evaluation.interrupted_branch_ids == ("audio",)
    assert "audio" in evaluation.unsettled_branch_ids
    assert outcome.work_id in evaluation.unsettled_work_ids


def test_inapplicable_branch_is_explicit_and_blocks_aggregate_success() -> None:
    plan, selections, settlements = settled_fixture()
    evaluation = evaluate_branch_set(
        plan,
        selections,
        branch_settlements=(settlements["audio"], settlements["video"]),
        branch_outcomes=(_branch_outcome(plan, "metadata", "inapplicable"),),
    )
    assert evaluation.inapplicable_branch_ids == ("metadata",)
    assert evaluation.unsettled_branch_ids == ()
    assert evaluation.branch_set_succeeded is False


def test_interrupted_join_remains_unsettled_and_resumable() -> None:
    plan, selections, settlements = settled_fixture()
    resolution = resolve_join_plan(plan, selections, tuple(settlements.values()))
    assert resolution is not None
    join_plan, _join_inputs = resolution
    outcome = JoinOutcome(
        work_id=join_plan.work.work_id,
        workflow_plan_sha256=join_plan.workflow_plan.workflow_plan_sha256,
        join_plan_sha256=join_plan.join_plan_sha256,
        state="interrupted",
    )
    evaluation = evaluate_branch_set(
        plan,
        selections,
        branch_settlements=tuple(settlements.values()),
        join_outcome=outcome,
    )
    assert evaluation.join_state == "interrupted"
    assert evaluation.unsettled_work_ids == (join_plan.work.work_id,)
    assert evaluation.branch_set_succeeded is False


def test_retirement_coordination_is_only_true_after_complete_success() -> None:
    plan, selections, settlements = settled_fixture()
    before = evaluate_branch_set(plan, selections, branch_settlements=tuple(settlements.values()))
    assert before.coordination_complete_for_retirement is False
    resolution = resolve_join_plan(plan, selections, tuple(settlements.values()))
    assert resolution is not None
    join_plan, join_inputs = resolution
    selections.update({item.selection_sha256: item for item in join_inputs})
    output_root = root(902, "retirement")
    output = ArtifactSelection.seal(
        (artifact("joined", output_root, "joined.bin", role="archive.joined/v1"),)
    )
    selections[output.selection_sha256] = output
    after = evaluate_branch_set(
        plan,
        selections,
        branch_settlements=tuple(settlements.values()),
        join_settlement=JoinSettlement.seal(
            plan=join_plan,
            derivation_sha256=digest("retirement-derivation"),
            output_collection=output_root,
            output_selection=output,
        ),
    )
    assert after.coordination_complete_for_retirement is True
    assert after.retirement_requested is True


def test_branch_set_retirement_grace_is_identity_bearing_and_policy_bound() -> None:
    plan, selections, _ = branch_set_fixture(with_join=False)
    delayed = BranchSetPlan.seal(
        parent_work=plan.parent_work,
        decision_sha256=plan.decision_sha256,
        evidence_sha256s=plan.evidence_sha256s,
        branches=plan.branches,
        retirement_policy="retire-after-verified-output",
        retirement_grace_seconds=3600,
        selections=selections,
    )
    assert delayed.branch_set_sha256 != plan.branch_set_sha256
    with pytest.raises(ValidationError, match="grace"):
        BranchSetPlan.seal(
            parent_work=plan.parent_work,
            decision_sha256=plan.decision_sha256,
            branches=plan.branches,
            retirement_policy="retain",
            retirement_grace_seconds=1,
            selections=selections,
        )


def test_unknown_duplicate_and_conflicting_branch_results_fail_closed() -> None:
    plan, selections, settlements = settled_fixture()
    unknown = settlements["video"].model_copy(update={"branch_id": "unknown"})
    with pytest.raises(ValueError, match="unknown branch"):
        evaluate_branch_set(plan, selections, branch_settlements=(unknown,))
    with pytest.raises(ValueError, match="duplicate settlement"):
        evaluate_branch_set(
            plan,
            selections,
            branch_settlements=(settlements["video"], settlements["video"]),
        )
    with pytest.raises(ValueError, match="both success"):
        evaluate_branch_set(
            plan,
            selections,
            branch_settlements=(settlements["video"],),
            branch_outcomes=(_branch_outcome(plan, "video", "failed"),),
        )


def test_stale_workflow_plan_result_is_rejected() -> None:
    plan, selections, settlements = settled_fixture()
    stale = settlements["video"].model_copy(update={"workflow_plan_sha256": digest("stale-plan")})
    with pytest.raises(ValueError, match="workflow plan"):
        evaluate_branch_set(plan, selections, branch_settlements=(stale,))


def test_duplicate_output_collection_roots_fail_closed() -> None:
    plan, selections, settlements = settled_fixture()
    audio = settlements["audio"]
    duplicate_selection = selections[settlements["video"].output_selection.selection_sha256]
    duplicate = BranchSettlement.seal(
        branch=next(item for item in plan.branches if item.branch_id == "video"),
        derivation_sha256=digest("duplicate"),
        output_collection=audio.output_collection,
        output_selection=ArtifactSelection.seal(
            tuple(
                item.model_copy(update={"collection": audio.output_collection})
                for item in duplicate_selection.artifacts
            )
        ),
    )
    selections[duplicate.output_selection.selection_sha256] = ArtifactSelection.seal(
        tuple(
            item.model_copy(update={"collection": audio.output_collection})
            for item in duplicate_selection.artifacts
        )
    )
    with pytest.raises(ValueError, match="same output collection"):
        evaluate_branch_set(
            plan,
            selections,
            branch_settlements=(audio, duplicate),
        )


def test_join_role_requirements_fail_closed() -> None:
    plan, selections, settlements = settled_fixture()
    bad_join = JoinDeclaration.seal(
        members=(
            JoinMemberDeclaration(branch_id="audio", output_roles=("archive.missing/v1",)),
            JoinMemberDeclaration(branch_id="video", output_roles=("archive.video/v1",)),
        ),
        recipe=recipe("join"),
        effective_intent={},
        workflow_intent=workflow_intent("join"),
    )
    bad_plan = BranchSetPlan.seal(
        parent_work=plan.parent_work,
        decision_sha256=plan.decision_sha256,
        evidence_sha256s=plan.evidence_sha256s,
        branches=plan.branches,
        join=bad_join,
        retirement_policy=plan.retirement_policy,
        selections=selections,
    )
    with pytest.raises(ValueError, match="lacks declared join role"):
        resolve_join_plan(bad_plan, selections, tuple(settlements.values()))


def test_join_outcome_must_bind_the_exact_resolved_plan() -> None:
    plan, selections, settlements = settled_fixture()
    resolution = resolve_join_plan(plan, selections, tuple(settlements.values()))
    assert resolution is not None
    join_plan, join_inputs = resolution
    selections.update({item.selection_sha256: item for item in join_inputs})
    stale = JoinOutcome(
        work_id=join_plan.work.work_id,
        workflow_plan_sha256=join_plan.workflow_plan.workflow_plan_sha256,
        join_plan_sha256=digest("stale-join-plan"),
        state="failed",
    )
    with pytest.raises(ValueError, match="does not bind"):
        evaluate_branch_set(
            plan,
            selections,
            branch_settlements=tuple(settlements.values()),
            join_outcome=stale,
        )


def test_join_output_cannot_reuse_a_branch_collection() -> None:
    plan, selections, settlements = settled_fixture()
    resolution = resolve_join_plan(plan, selections, tuple(settlements.values()))
    assert resolution is not None
    join_plan, join_inputs = resolution
    selections.update({item.selection_sha256: item for item in join_inputs})
    branch_output = selections[settlements["audio"].output_selection.selection_sha256]
    join_settlement = JoinSettlement.seal(
        plan=join_plan,
        derivation_sha256=digest("bad-join"),
        output_collection=settlements["audio"].output_collection,
        output_selection=branch_output,
    )
    with pytest.raises(ValueError, match="additional retained collection"):
        evaluate_branch_set(
            plan,
            selections,
            branch_settlements=tuple(settlements.values()),
            join_settlement=join_settlement,
        )


def test_selection_outside_parent_roots_is_rejected() -> None:
    plan, selections, branches = branch_set_fixture(with_join=False)
    outside = ArtifactSelection.seal((artifact("outside", root(99), "outside.bin"),))
    selections[outside.selection_sha256] = outside
    bad = BranchPlan.build(
        parent_work=plan.parent_work,
        branch_id="outside",
        decision_sha256=plan.decision_sha256,
        selection=outside,
        recipe=recipe("outside"),
        effective_intent={},
        workflow_intent=workflow_intent("outside"),
    )
    with pytest.raises(ValueError, match="outside"):
        BranchSetPlan.seal(
            parent_work=plan.parent_work,
            decision_sha256=plan.decision_sha256,
            branches=(branches["video"], bad),
            selections=selections,
        )


def test_large_branch_and_join_set_is_deterministic_without_protocol_ceiling() -> None:
    source = root(1)
    parent = parent_work(source)
    decision = digest("large-plan")
    selections: dict[str, ArtifactSelection] = {}
    branches: list[BranchPlan] = []
    members: list[JoinMemberDeclaration] = []
    for index in range(256):
        selection = ArtifactSelection.seal(
            (artifact(f"a-{index:03d}", source, f"source/{index:03d}.bin"),)
        )
        selections[selection.selection_sha256] = selection
        branches.append(
            branch(
                parent=parent,
                branch_id=f"b-{index:03d}",
                decision=decision,
                selection=selection,
            )
        )
        members.append(
            JoinMemberDeclaration(
                branch_id=f"b-{index:03d}",
                output_roles=("archive.part/v1",),
            )
        )
    join = JoinDeclaration.seal(
        members=tuple(reversed(members)),
        recipe=recipe("large-join"),
        effective_intent={"kind": "large-proof"},
        workflow_intent=workflow_intent("large-join"),
    )
    plan = BranchSetPlan.seal(
        parent_work=parent,
        decision_sha256=decision,
        branches=tuple(reversed(branches)),
        join=join,
        selections=selections,
    )
    rebuilt = BranchSetPlan.seal(
        parent_work=parent,
        decision_sha256=decision,
        branches=branches,
        join=join,
        selections=selections,
    )
    assert len(plan.branches) == 256
    assert plan.branch_set_sha256 == rebuilt.branch_set_sha256

    settlements: list[BranchSettlement] = []
    for index, branch_plan in enumerate(plan.branches):
        output = root(1_000 + index, f"large-output:{index}")
        output_selection = ArtifactSelection.seal(
            (
                artifact(
                    f"output-{index:03d}",
                    output,
                    f"output/{index:03d}.bin",
                    role="archive.part/v1",
                ),
            )
        )
        selections[output_selection.selection_sha256] = output_selection
        settlements.append(
            BranchSettlement.seal(
                branch=branch_plan,
                derivation_sha256=digest(f"large-derivation:{index}"),
                output_collection=output,
                output_selection=output_selection,
            )
        )

    first = evaluate_branch_set(
        plan,
        selections,
        branch_settlements=tuple(settlements),
    )
    second = evaluate_branch_set(
        plan,
        selections,
        branch_settlements=tuple(reversed(settlements)),
    )
    assert first == second
    assert first.join_ready is True
    assert first.resolved_join_plan is not None
    assert len(first.resolved_join_plan.inputs) == 256
    assert first.unsettled_work_ids == (first.resolved_join_plan.work.work_id,)


def test_join_requires_two_members() -> None:
    with pytest.raises(ValidationError):
        JoinDeclaration.seal(
            members=(
                JoinMemberDeclaration(
                    branch_id="only",
                    output_roles=("archive.only/v1",),
                ),
            ),
            recipe=recipe("join"),
            effective_intent={},
            workflow_intent=workflow_intent("join"),
        )


def test_evaluation_bound_parent_forces_retain() -> None:
    plan, selections, _ = branch_set_fixture(with_join=False, evaluation=True)
    with pytest.raises(ValidationError, match="retain"):
        BranchSetPlan.seal(
            parent_work=plan.parent_work,
            decision_sha256=plan.decision_sha256,
            branches=plan.branches,
            retirement_policy="retire-after-verified-output",
            selections=selections,
        )


def test_branch_plan_serialization_contains_refs_not_full_selection_documents() -> None:
    plan, _, _ = branch_set_fixture()
    encoded = plan.canonical_bytes()
    assert b'"selection_sha256"' in encoded
    assert b'"artifacts"' not in encoded
    assert b"source/shared.mov" not in encoded


def test_kernel_import_boundary_and_no_concrete_io_calls() -> None:
    source = Path(__file__).parents[1] / "src" / "stove0_protocol" / "fork_join.py"
    tree = ast.parse(source.read_text())
    forbidden_roots = {
        "asyncio",
        "fastapi",
        "httpx",
        "os",
        "pathlib",
        "requests",
        "socket",
        "sqlalchemy",
        "subprocess",
        "time",
        "urllib",
    }
    imports: set[str] = set()
    calls: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".", 1)[0])
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                calls.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                calls.add(node.func.attr)
    assert not imports.intersection(forbidden_roots)
    assert not calls.intersection({"open", "exec", "eval", "system", "popen", "run"})


def test_canonical_json_reconstruction_is_stable() -> None:
    selection = ArtifactSelection.seal(
        (
            artifact("b", root(2), "b.bin"),
            artifact("a", root(1), "a.bin"),
        )
    )
    reconstructed = ArtifactSelection.model_validate_json(selection.canonical_bytes())
    assert reconstructed == selection
    assert (
        canonical_json_bytes(reconstructed.model_dump(mode="json", exclude_none=True))
        == selection.canonical_bytes()
    )

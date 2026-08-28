from __future__ import annotations

from pathlib import Path

import pytest
from riverhog_protocol.collection_workflows import (
    ArtifactDisposition,
    CollectionDerivation,
)
from riverhog_protocol.collection_workflows import (
    canonical_json_sha256 as riverhog_canonical_json_sha256,
)
from sqlalchemy import text
from stove0_core import (
    ClaimBinding,
    ConcurrentWorkUpdate,
    InMemoryWorkStore,
    Stove0StateError,
    Stove0WorkService,
    WorkFailure,
    WorkInapplicable,
    WorkRecord,
)
from stove0_observer_protocol import (
    ObservationEvidence,
    ObservationFailure,
    ObservationInapplicable,
    ObservationRequest,
    ObservationRequestPayload,
    ObservationResult,
    ObservationResultPayload,
    ObserverContract,
    ObserverContractPayload,
    ObserverContractSupport,
    ObserverDescriptor,
    ObserverDescriptorPayload,
    ObserverImplementation,
)
from stove0_protocol import (
    JSON_SCHEMA_ONLY_SEMANTIC_PROFILE,
    ArtifactSelection,
    ArtifactSubject,
    BranchPlan,
    BranchSetDecision,
    BranchSetPlan,
    CollectionRootRef,
    CoordinationBranchPlan,
    JsonSchemaDocument,
    OperationRef,
    RecipeRef,
    WorkflowPlan,
    WorkflowPlanIntent,
    WorkflowPlanPayload,
    WorkIdentity,
    WorkPayload,
    canonical_json_sha256,
)
from stove0_target_support import (
    InputArtifact,
    InputArtifactContract,
    OperationContract,
    OperationContractPayload,
    OutputArtifact,
    OutputArtifactContract,
    OutputCollectionRef,
    TargetContract,
    TargetContractPayload,
    TargetExecutionEvidence,
    TargetJobDeclaration,
    TargetJobRequest,
    TargetJobStatus,
    TargetOperationSupport,
    TargetProgress,
    TargetRuntimeAuthority,
    TransformPlan,
    TransformPlanPayload,
)


def _sha(character: str) -> str:
    return character * 64


def _root() -> CollectionRootRef:
    return CollectionRootRef(
        collection_id=1,
        archive_root_sha256=_sha("1"),
        content_identity=_sha("2"),
    )


def _work() -> WorkIdentity:
    return WorkIdentity.seal(
        WorkPayload(
            recipe=RecipeRef(id="fixture.recipe/v1", revision=1, sha256=_sha("3")),
            inputs=(_root(),),
            effective_intent={"suffix": ".copy"},
        )
    )


def _observer() -> tuple[ObserverContract, ObserverDescriptor]:
    contract = ObserverContract.seal(
        ObserverContractPayload(
            id="fixture.kind/v1",
            facts_semantics=JSON_SCHEMA_ONLY_SEMANTIC_PROFILE,
            options_schema=JsonSchemaDocument.from_schema(
                "fixture.kind-options/v1",
                {"type": "object", "additionalProperties": False},
            ),
            facts_schema=JsonSchemaDocument.from_schema(
                "fixture.kind-facts/v1",
                {
                    "type": "object",
                    "properties": {"kind": {"type": "string"}},
                    "required": ["kind"],
                    "additionalProperties": False,
                },
            ),
        )
    )
    descriptor = ObserverDescriptor.seal(
        ObserverDescriptorPayload(
            implementation_id="fixture.observer/v1",
            implementation_version="1.0.0",
            source_revision="fixture",
            image_digest=_sha("9"),
            contracts=(ObserverContractSupport.from_contract(contract),),
        )
    )
    return contract, descriptor


def _observation(
    work: WorkIdentity,
    contract: ObserverContract,
    descriptor: ObserverDescriptor,
) -> tuple[ObservationRequest, ObservationResult]:
    subject = ArtifactSubject(
        id="source",
        role="fixture.source/v1",
        collection=_root(),
        path="source/input.bin",
        bytes=12,
        sha256=_sha("4"),
    )
    request = ObservationRequest.seal(
        ObservationRequestPayload(
            work_id=work.work_id,
            observer_registration_id="fixture-observer",
            observer_descriptor_sha256=descriptor.descriptor_sha256,
            observer_contract_id=contract.id,
            observer_contract_sha256=contract.contract_sha256,
            subjects=(subject,),
            options={},
        )
    )
    facts = {"kind": "fixture"}
    result = ObservationResult.seal(
        ObservationResultPayload(
            request_id=request.request_id,
            state="observed",
            observer=ObserverImplementation(
                id=descriptor.implementation_id,
                version=descriptor.implementation_version,
                source_revision=descriptor.source_revision,
                descriptor_sha256=descriptor.descriptor_sha256,
            ),
            observer_contract_id=contract.id,
            observer_contract_sha256=contract.contract_sha256,
            subjects=request.subjects,
            facts_schema=contract.facts_schema,
            facts=facts,
            facts_sha256=canonical_json_sha256(facts),
        )
    )
    return request, result


def _operation() -> OperationContract:
    return OperationContract.seal(
        OperationContractPayload(
            id="fixture.copy/v1",
            intent_semantics=JSON_SCHEMA_ONLY_SEMANTIC_PROFILE,
            intent_schema=JsonSchemaDocument.from_schema(
                "fixture.copy-intent/v1",
                {
                    "type": "object",
                    "properties": {"suffix": {"type": "string"}},
                    "required": ["suffix"],
                    "additionalProperties": False,
                },
            ),
            inputs=(
                InputArtifactContract(
                    role="fixture.source/v1",
                    allowed_dispositions=("transformed",),
                ),
            ),
            outputs=(
                OutputArtifactContract(
                    role="fixture.output/v1",
                    derived_from_roles=("fixture.source/v1",),
                ),
            ),
        )
    )


def _target(operation: OperationContract) -> TargetContract:
    return TargetContract.seal(
        TargetContractPayload(
            implementation_id="fixture.target/v1",
            implementation_version="1.0.0",
            source_revision="fixture",
            image_digest=_sha("9"),
            operations=(
                TargetOperationSupport(
                    operation_id=operation.id,
                    operation_contract_sha256=operation.contract_sha256,
                    options_schema=JsonSchemaDocument.from_schema(
                        "fixture.target-options/v1",
                        {"type": "object", "additionalProperties": False},
                    ),
                ),
            ),
        )
    )


def _target_plan(
    operation: OperationContract,
    target: TargetContract,
    *,
    observation_result_sha256s: tuple[str, ...] = (),
) -> TransformPlan:
    return TransformPlan.seal(
        TransformPlanPayload(
            target_implementation_id=target.implementation_id,
            target_contract_sha256=target.contract_sha256,
            operation_id=operation.id,
            operation_contract_sha256=operation.contract_sha256,
            inputs=(
                InputArtifact(
                    id="source",
                    role="fixture.source/v1",
                    collection=_root(),
                    path="source/input.bin",
                    bytes=12,
                    sha256=_sha("4"),
                ),
            ),
            intent={"suffix": ".copy"},
            target_options={},
            observation_result_sha256s=observation_result_sha256s,
        )
    )


def _branch_decision(work: WorkIdentity) -> BranchSetDecision:
    operation = _operation()
    target = _target(operation)
    selection = ArtifactSelection.seal(
        (
            ArtifactSubject(
                id="source",
                role="fixture.source/v1",
                collection=_root(),
                path="source/input.bin",
                bytes=12,
                sha256=_sha("4"),
            ),
        )
    )
    branch = BranchPlan.build(
        parent_work=work,
        branch_id="fixture",
        decision_sha256=_sha("d"),
        selection=selection,
        recipe=work.recipe,
        effective_intent=work.effective_intent,
        workflow_intent=WorkflowPlanIntent(
            operation=OperationRef(id=operation.id, sha256=operation.contract_sha256),
            target_registration_id="fixture-target",
            target_contract_sha256=target.contract_sha256,
            output_tags=("fixture-output",),
            retirement_policy="retain",
        ),
    )
    return BranchSetDecision(
        plan=BranchSetPlan.seal(
            parent_work=work,
            decision_sha256=_sha("d"),
            branches=(branch,),
            selections={selection.selection_sha256: selection},
        ),
        selections=(selection,),
    )


def _nested_branch_decision(work: WorkIdentity) -> BranchSetDecision:
    operation = _operation()
    target = _target(operation)
    selection = ArtifactSelection.seal(
        (
            ArtifactSubject(
                id="source",
                role="fixture.source/v1",
                collection=_root(),
                path="source/input.bin",
                bytes=12,
                sha256=_sha("4"),
            ),
        )
    )
    child_work = CoordinationBranchPlan.build_work(
        parent_work=work,
        branch_id="nested",
        decision_sha256=_sha("d"),
        selection=selection,
        recipe=RecipeRef(id="fixture.child/v1", revision=1, sha256=_sha("5")),
        effective_intent={"scope": "child"},
    )
    leaf = BranchPlan.build(
        parent_work=child_work,
        branch_id="leaf",
        decision_sha256=_sha("e"),
        selection=selection,
        recipe=child_work.recipe,
        effective_intent=child_work.effective_intent,
        workflow_intent=WorkflowPlanIntent(
            operation=OperationRef(id=operation.id, sha256=operation.contract_sha256),
            target_registration_id="fixture-target",
            target_contract_sha256=target.contract_sha256,
            output_tags=("fixture-output",),
            retirement_policy="retain",
        ),
    )
    child_plan = BranchSetPlan.seal(
        parent_work=child_work,
        decision_sha256=_sha("e"),
        branches=(leaf,),
        selections={selection.selection_sha256: selection},
    )
    nested = CoordinationBranchPlan(
        branch_id="nested",
        artifact_selection=selection.ref(),
        work=child_work,
        branch_set_sha256=child_plan.branch_set_sha256,
    )
    root_plan = BranchSetPlan.seal(
        parent_work=work,
        decision_sha256=_sha("d"),
        branches=(nested,),
        selections={selection.selection_sha256: selection},
        branch_sets={child_plan.branch_set_sha256: child_plan},
    )
    return BranchSetDecision(
        plan=root_plan,
        selections=(selection,),
        branch_sets=(child_plan,),
    )


def test_one_record_carries_observation_plan_execution_verification_and_completion() -> None:
    store = InMemoryWorkStore()
    service = Stove0WorkService(store)
    work = _work()
    operation = _operation()
    target = _target(operation)
    contract, descriptor = _observer()
    request, result = _observation(work, contract, descriptor)

    record = service.create_or_resume(work)
    assert service.create_or_resume(work) == record
    record = service.bind_claim(
        work.work_id,
        claim_id=work.work_id,
        fence=1,
        expected_revision=record.revision,
    )
    record = service.begin_observations(
        work.work_id,
        (request,),
        expected_revision=record.revision,
    )
    record = service.record_observation(
        work.work_id,
        result,
        expected_revision=record.revision,
    )
    assert record.phase == "planning"

    workflow = WorkflowPlan.seal(
        WorkflowPlanPayload(
            work=work,
            observations=(ObservationEvidence(request=request, result=result),),
            operation=OperationRef(id=operation.id, sha256=operation.contract_sha256),
            target_registration_id="fixture-target",
            target_contract_sha256=target.contract_sha256,
            output_tags=("fixture-output",),
            retirement_policy="retain",
        )
    )
    record = service.seal_workflow_plan(
        work.work_id,
        workflow,
        expected_revision=record.revision,
    )
    plan = _target_plan(
        operation,
        target,
        observation_result_sha256s=(result.result_sha256,),
    )
    record = service.seal_target_plan(
        work.work_id,
        target=target,
        plan=plan,
        expected_revision=record.revision,
    )
    assert record.controller_evidence is not None
    declaration = TargetJobDeclaration(
        job_id=record.controller_evidence.execution_envelope.execution_envelope_sha256,
        claim_id=work.work_id,
        fence=1,
        controller_evidence=record.controller_evidence,
        plan=plan,
        workspace_assurance="ephemeral",
    )
    target_request = TargetJobRequest.seal(
        declaration,
        TargetRuntimeAuthority(
            riverhog_base_url="https://riverhog.invalid",
            capability_token="secret",
        ),
    )
    record = service.bind_target_request(
        work.work_id,
        target_request,
        expected_revision=record.revision,
    )
    running = TargetJobStatus(
        job_id=declaration.job_id,
        state="running",
        attempt=1,
        request_sha256=target_request.request_sha256,
        plan_sha256=plan.plan_sha256,
        progress=TargetProgress(phase="transform", completed=1, total=2),
    )
    record = service.record_target_status(
        work.work_id,
        running,
        operation=operation,
        expected_revision=record.revision,
    )
    output = OutputArtifact(
        id="output",
        role="fixture.output/v1",
        path="output/result.bin",
        bytes=12,
        sha256=_sha("5"),
        derived_from=("source",),
    )
    assert record.controller_evidence is not None
    workflow = record.controller_evidence.execution_envelope.workflow_plan
    derivation = CollectionDerivation(
        execution_id=declaration.job_id,
        claim_id=declaration.claim_id,
        fence=declaration.fence,
        recipe=workflow.work.recipe.to_identity(),
        operation=workflow.operation.to_identity(),
        inputs=tuple(item.to_identity() for item in workflow.work.inputs),
        output_tags=workflow.output_tags,
        execution_envelope_sha256=declaration.job_id,
        execution_sha256=_sha("9"),
        controller_evidence=record.controller_evidence.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        ),
        controller_evidence_sha256=riverhog_canonical_json_sha256(
            record.controller_evidence.model_dump(mode="json", by_alias=True, exclude_none=True)
        ),
        dispositions=(
            ArtifactDisposition(
                input_collection_id=_root().collection_id,
                input_archive_root_sha256=_root().archive_root_sha256,
                input_path=plan.inputs[0].path,
                status="transformed",
                outputs=(output.path,),
            ),
        ),
    )
    output_collection = OutputCollectionRef(
        collection_id=7,
        archive_root_sha256=_sha("6"),
        content_identity=_sha("7"),
        derivation_sha256=derivation.sha256,
    )
    succeeded = TargetJobStatus(
        job_id=declaration.job_id,
        state="succeeded",
        attempt=1,
        request_sha256=target_request.request_sha256,
        plan_sha256=plan.plan_sha256,
        progress=TargetProgress(phase="done", completed=2, total=2),
        outputs=(output,),
        output_collection=output_collection,
        execution_evidence=TargetExecutionEvidence(
            target_contract_sha256=target.contract_sha256,
            operation_contract_sha256=operation.contract_sha256,
            plan_sha256=plan.plan_sha256,
            execution_sha256=_sha("9"),
        ),
        derivation=derivation.as_dict(),
    )
    record = service.record_target_status(
        work.work_id,
        succeeded,
        operation=operation,
        expected_revision=record.revision,
    )
    assert record.phase == "verifying"
    record = service.verify_output(
        work.work_id,
        output_collection,
        expected_revision=record.revision,
    )
    assert record.phase == "settled"
    record = service.begin_retirement(
        work.work_id,
        (),
        expected_revision=record.revision,
    )
    assert record.phase == "complete"


def test_new_claim_fence_resets_unsettled_execution_authorities() -> None:
    service = Stove0WorkService(InMemoryWorkStore())
    work = _work()
    record = service.create_or_resume(work)
    record = service.bind_claim(
        work.work_id,
        claim_id=work.work_id,
        fence=1,
        expected_revision=record.revision,
    )
    record = service.begin_planning(work.work_id, expected_revision=record.revision)
    operation = _operation()
    target = _target(operation)
    workflow = WorkflowPlan.seal(
        WorkflowPlanPayload(
            work=work,
            operation=OperationRef(id=operation.id, sha256=operation.contract_sha256),
            target_registration_id="fixture-target",
            target_contract_sha256=target.contract_sha256,
            output_tags=("fixture-output",),
            retirement_policy="retain",
        )
    )
    record = service.seal_workflow_plan(
        work.work_id,
        workflow,
        expected_revision=record.revision,
    )
    record = service.seal_target_plan(
        work.work_id,
        target=target,
        plan=_target_plan(operation, target),
        expected_revision=record.revision,
    )
    stale_execution_id = record.controller_evidence.execution_envelope.execution_envelope_sha256

    rebound = service.rebind_claim(
        work.work_id,
        claim_id=work.work_id,
        fence=2,
        expected_revision=record.revision,
    )

    assert rebound.phase == "claimed"
    assert rebound.claim == ClaimBinding(claim_id=work.work_id, fence=2)
    assert rebound.workflow_plan is None
    assert rebound.target_plan is None
    assert rebound.controller_evidence is None

    rebound = service.begin_planning(work.work_id, expected_revision=rebound.revision)
    rebound = service.seal_workflow_plan(
        work.work_id,
        workflow,
        expected_revision=rebound.revision,
    )
    rebound = service.seal_target_plan(
        work.work_id,
        target=target,
        plan=_target_plan(operation, target),
        expected_revision=rebound.revision,
    )
    assert rebound.controller_evidence is not None
    assert rebound.controller_evidence.execution_envelope.fence == 2
    assert (
        rebound.controller_evidence.execution_envelope.execution_envelope_sha256
        != stale_execution_id
    )


@pytest.mark.parametrize(
    ("state", "outcome", "expected_phase", "expected_abandon"),
    [
        (
            "inapplicable",
            {"inapplicable": ObservationInapplicable(code="unsupported", message="No match")},
            "abandon_pending",
            "inapplicable",
        ),
        (
            "failed",
            {"failure": ObservationFailure(code="temporary", message="Try again", retryable=True)},
            "failed",
            None,
        ),
        (
            "failed",
            {
                "failure": ObservationFailure(
                    code="invalid", message="Cannot inspect", retryable=False
                )
            },
            "abandon_pending",
            "failed",
        ),
        ("canceled", {}, "abandon_pending", "canceled"),
    ],
)
def test_terminal_observation_results_converge_without_entering_planning(
    state: str,
    outcome: dict[str, object],
    expected_phase: str,
    expected_abandon: str | None,
) -> None:
    service = Stove0WorkService(InMemoryWorkStore())
    work = _work()
    contract, descriptor = _observer()
    request, observed = _observation(work, contract, descriptor)
    record = service.create_or_resume(work)
    record = service.bind_claim(
        work.work_id,
        claim_id=work.work_id,
        fence=1,
        expected_revision=record.revision,
    )
    record = service.begin_observations(
        work.work_id,
        (request,),
        expected_revision=record.revision,
    )
    result = ObservationResult.seal(
        ObservationResultPayload(
            **observed.model_dump(
                mode="python",
                exclude_none=True,
                exclude={
                    "result_sha256",
                    "state",
                    "facts_schema",
                    "facts",
                    "facts_sha256",
                },
            ),
            state=state,  # type: ignore[arg-type]
            **outcome,
        )
    )

    record = service.record_observation(
        work.work_id,
        result,
        expected_revision=record.revision,
    )

    assert record.phase == expected_phase
    assert record.observation_results == (result,)
    assert record.abandon_outcome == expected_abandon


def test_stale_revision_and_invalid_success_order_fail_closed() -> None:
    service = Stove0WorkService(InMemoryWorkStore())
    record = service.create_or_resume(_work())
    claimed = service.bind_claim(
        record.work_id,
        claim_id=record.work_id,
        fence=1,
        expected_revision=record.revision,
    )
    with pytest.raises(ConcurrentWorkUpdate):
        service.begin_planning(record.work_id, expected_revision=record.revision)
    with pytest.raises(Stove0StateError, match="verify output"):
        service.verify_output(
            record.work_id,
            OutputCollectionRef(
                collection_id=7,
                archive_root_sha256=_sha("6"),
                content_identity=_sha("7"),
                derivation_sha256=_sha("8"),
            ),
            expected_revision=claimed.revision,
        )


@pytest.mark.parametrize(
    ("transition", "terminal"),
    [
        ("cancel", "canceled"),
        ("inapplicable", "inapplicable"),
        ("failed", "failed"),
    ],
)
def test_no_output_terminal_work_is_crash_safe_through_abandon_pending(
    transition: str,
    terminal: str,
) -> None:
    service = Stove0WorkService(InMemoryWorkStore())
    record = service.create_or_resume(_work())
    record = service.bind_claim(
        record.work_id,
        claim_id=record.work_id,
        fence=1,
        expected_revision=record.revision,
    )
    if transition == "cancel":
        record = service.cancel(record.work_id, expected_revision=record.revision)
    elif transition == "inapplicable":
        record = service.mark_inapplicable(
            record.work_id,
            WorkInapplicable(code="not-applicable", message="fixture outcome"),
            expected_revision=record.revision,
        )
    else:
        record = service.fail(
            record.work_id,
            WorkFailure(code="terminal", message="fixture failure", retryable=False),
            expected_revision=record.revision,
        )

    assert record.phase == "abandon_pending"
    assert record.abandon_outcome == terminal
    completed = service.complete_abandon(
        record.work_id,
        expected_revision=record.revision,
    )
    assert completed.phase == terminal
    assert completed.abandon_outcome is None


def test_retryable_failed_work_requires_a_new_fencing_generation() -> None:
    service = Stove0WorkService(InMemoryWorkStore())
    record = service.create_or_resume(_work())
    record = service.bind_claim(
        record.work_id,
        claim_id=record.work_id,
        fence=1,
        expected_revision=record.revision,
    )
    record = service.fail(
        record.work_id,
        WorkFailure(code="temporary", message="retry later", retryable=True),
        expected_revision=record.revision,
    )
    with pytest.raises(Stove0StateError, match="advance the Riverhog claim fence"):
        service.retry_failed(
            record.work_id,
            claim_id=record.work_id,
            fence=1,
            expected_revision=record.revision,
        )

    retried = service.retry_failed(
        record.work_id,
        claim_id=record.work_id,
        fence=2,
        expected_revision=record.revision,
    )
    assert retried.phase == "claimed"
    assert retried.claim == ClaimBinding(claim_id=record.work_id, fence=2)
    assert retried.failure is None


def test_retryable_failed_work_can_be_canceled_and_abandoned() -> None:
    service = Stove0WorkService(InMemoryWorkStore())
    record = service.create_or_resume(_work())
    record = service.bind_claim(
        record.work_id,
        claim_id=record.work_id,
        fence=1,
        expected_revision=record.revision,
    )
    record = service.fail(
        record.work_id,
        WorkFailure(code="temporary", message="retry later", retryable=True),
        expected_revision=record.revision,
    )
    assert record.phase == "failed"
    assert record.failure is not None and record.failure.retryable

    pending = service.cancel(record.work_id, expected_revision=record.revision)
    assert pending.phase == "abandon_pending"
    assert pending.failure is None
    assert pending.abandon_outcome == "canceled"

    terminal = service.complete_abandon(
        pending.work_id,
        expected_revision=pending.revision,
    )
    assert terminal.phase == "canceled"


def test_unified_state_store_is_restart_safe_and_compare_and_swap(tmp_path: Path) -> None:
    from stove0_core import SqlAlchemyStateStore

    path = tmp_path / "private" / "stove0.sqlite3"
    store = SqlAlchemyStateStore(f"sqlite+pysqlite:///{path}")
    service = Stove0WorkService(store)
    created = service.create_or_resume(_work())
    claimed = service.bind_claim(
        created.work_id,
        claim_id=created.work_id,
        fence=1,
        expected_revision=created.revision,
    )

    restarted = SqlAlchemyStateStore(f"sqlite+pysqlite:///{path}")
    assert restarted.load(created.work_id) == claimed
    events = restarted.list_events().events
    assert [event.type for event in events] == [
        "io.riverhog.stove0.work.created",
        "io.riverhog.stove0.work.updated",
    ]
    assert events[0].data["work_id"] == created.work_id
    assert events[1].data["revision"] == claimed.revision

    with pytest.raises(ConcurrentWorkUpdate, match="stale stove0 work revision"):
        store.compare_and_swap(
            created.work_id,
            expected_revision=created.revision,
            replacement=claimed,
        )


def test_sql_runnable_scan_ignores_terminal_history_and_uses_a_keyset(
    tmp_path: Path,
) -> None:
    from stove0_core import SqlAlchemyStateStore

    store = SqlAlchemyStateStore(f"sqlite+pysqlite:///{tmp_path / 'scan.sqlite3'}")
    for index in range(250):
        identity = WorkIdentity.seal(
            WorkPayload(
                recipe=RecipeRef(id="fixture.recipe/v1", revision=1, sha256=_sha("3")),
                inputs=(
                    CollectionRootRef(
                        collection_id=index + 1,
                        archive_root_sha256=f"{index + 1:064x}",
                        content_identity=_sha("2"),
                    ),
                ),
            )
        )
        store.create(WorkRecord(work=identity, phase="canceled"))
    runnable = store.create(WorkRecord(work=_work()))

    records, cursor = store.scan_work(
        phases=("eligible",),
        after_work_id="",
        limit=1,
    )

    assert records == [runnable]
    assert cursor == runnable.work_id


def test_sql_operational_retention_prunes_only_complete_expired_components(
    tmp_path: Path,
) -> None:
    from stove0_core import SqlAlchemyStateStore

    store = SqlAlchemyStateStore(f"sqlite+pysqlite:///{tmp_path / 'retention.sqlite3'}")
    service = Stove0WorkService(store)
    work = _work()
    parent = service.create_or_resume(work)
    parent = service.bind_claim(
        parent.work_id,
        claim_id=parent.work_id,
        fence=1,
        expected_revision=parent.revision,
    )
    parent = service.begin_planning(parent.work_id, expected_revision=parent.revision)
    decision = _branch_decision(work)
    parent = service.admit_branch_set(
        parent.work_id,
        decision,
        expected_revision=parent.revision,
    )
    child_id = decision.plan.branches[0].workflow_plan.work.work_id
    child = store.load(child_id)
    assert child is not None
    child = WorkRecord.model_validate(
        child.model_copy(update={"phase": "canceled", "revision": 2}).model_dump(mode="python")
    )
    store.compare_and_swap(
        child_id,
        expected_revision=1,
        replacement=child,
    )
    parent = WorkRecord.model_validate(
        parent.model_copy(update={"phase": "complete", "revision": parent.revision + 1}).model_dump(
            mode="python"
        )
    )
    store.compare_and_swap(
        parent.work_id,
        expected_revision=parent.revision - 1,
        replacement=parent,
    )
    old = "2000-01-01T00:00:00.000000Z"
    cutoff = "2001-01-01T00:00:00.000000Z"
    with store.engine.begin() as connection:
        connection.execute(
            text("UPDATE stove0_work_records SET updated_at = :old WHERE work_id = :work_id"),
            {"old": old, "work_id": parent.work_id},
        )

    retained = store.prune_operational_state(cutoff=cutoff)
    assert retained["work"] == 0
    assert store.load(parent.work_id) == parent
    assert store.load(child_id) == child
    selection = decision.selections[0]
    assert store.load_selection(selection.selection_sha256) == selection

    with store.engine.begin() as connection:
        connection.execute(
            text("UPDATE stove0_work_records SET updated_at = :old WHERE work_id = :work_id"),
            {"old": old, "work_id": child_id},
        )
        connection.execute(
            text("UPDATE stove0_lifecycle_events SET created_at = :old"),
            {"old": old},
        )

    pruned = store.prune_operational_state(cutoff=cutoff)
    assert pruned["work"] == 2
    assert pruned["work_bytes"] > 0
    assert pruned["selections"] == 1
    assert pruned["events"] > 0
    assert store.list_work()["total"] == 0
    assert store.load_selection(selection.selection_sha256) is None


def test_sql_branch_set_admission_is_restart_safe_and_exposes_exact_children(
    tmp_path: Path,
) -> None:
    from stove0_core import SqlAlchemyStateStore

    path = tmp_path / "private" / "stove0.sqlite3"
    store = SqlAlchemyStateStore(f"sqlite+pysqlite:///{path}")
    service = Stove0WorkService(store)
    work = _work()
    record = service.create_or_resume(work)
    record = service.bind_claim(
        record.work_id,
        claim_id=record.work_id,
        fence=1,
        expected_revision=record.revision,
    )
    record = service.begin_planning(record.work_id, expected_revision=record.revision)
    decision = _branch_decision(work)

    admitted = service.admit_branch_set(
        record.work_id,
        decision,
        expected_revision=record.revision,
    )

    assert admitted.phase == "coordinating"
    assert admitted.branch_set_plan == decision.plan
    branch = decision.plan.branches[0]
    child = store.load(branch.workflow_plan.work.work_id)
    assert child == WorkRecord(
        work=branch.workflow_plan.work,
        workflow_plan=branch.workflow_plan,
    )
    selection = decision.selections[0]
    assert store.load_selection(selection.selection_sha256) == selection

    restarted = SqlAlchemyStateStore(f"sqlite+pysqlite:///{path}")
    assert restarted.load(work.work_id) == admitted
    assert restarted.load(child.work_id) == child
    assert restarted.load_selection(selection.selection_sha256) == selection


def test_sql_nested_admission_atomically_normalizes_coordinator_and_leaf_records(
    tmp_path: Path,
) -> None:
    from stove0_core import SqlAlchemyStateStore

    path = tmp_path / "private" / "stove0.sqlite3"
    store = SqlAlchemyStateStore(f"sqlite+pysqlite:///{path}")
    service = Stove0WorkService(store)
    work = _work()
    record = service.create_or_resume(work)
    record = service.bind_claim(
        record.work_id,
        claim_id=record.work_id,
        fence=1,
        expected_revision=record.revision,
    )
    record = service.begin_planning(record.work_id, expected_revision=record.revision)
    decision = _nested_branch_decision(work)

    admitted = service.admit_branch_set(
        record.work_id,
        decision,
        expected_revision=record.revision,
    )

    nested = decision.plan.branches[0]
    assert isinstance(nested, CoordinationBranchPlan)
    child_plan = decision.branch_set_documents[nested.branch_set_sha256]
    coordinator = store.load(nested.work.work_id)
    assert coordinator == WorkRecord(work=nested.work, branch_set_plan=child_plan)
    leaf = child_plan.branches[0]
    assert isinstance(leaf, BranchPlan)
    leaf_record = store.load(leaf.workflow_plan.work.work_id)
    assert leaf_record == WorkRecord(
        work=leaf.workflow_plan.work,
        workflow_plan=leaf.workflow_plan,
    )
    coordinator = service.bind_claim(
        coordinator.work_id,
        claim_id=coordinator.work_id,
        fence=1,
        expected_revision=coordinator.revision,
    )
    coordinator = service.activate_preplanned_coordination(
        coordinator.work_id,
        expected_revision=coordinator.revision,
    )
    assert coordinator.phase == "coordinating"

    restarted = SqlAlchemyStateStore(f"sqlite+pysqlite:///{path}")
    assert restarted.load(work.work_id) == admitted
    assert restarted.load(coordinator.work_id) == coordinator
    assert restarted.load(leaf_record.work_id) == leaf_record
    assert (
        restarted.load_selection(decision.selections[0].selection_sha256)
        == (decision.selections[0])
    )


def test_sql_branch_set_admission_rolls_back_every_document_on_child_conflict(
    tmp_path: Path,
) -> None:
    from stove0_core import SqlAlchemyStateStore

    path = tmp_path / "private" / "stove0.sqlite3"
    store = SqlAlchemyStateStore(f"sqlite+pysqlite:///{path}")
    service = Stove0WorkService(store)
    work = _work()
    record = service.create_or_resume(work)
    record = service.bind_claim(
        record.work_id,
        claim_id=record.work_id,
        fence=1,
        expected_revision=record.revision,
    )
    record = service.begin_planning(record.work_id, expected_revision=record.revision)
    decision = _branch_decision(work)
    branch = decision.plan.branches[0]
    conflicting_plan = WorkflowPlanIntent(
        operation=branch.workflow_plan.operation,
        target_registration_id=branch.workflow_plan.target_registration_id,
        target_contract_sha256=branch.workflow_plan.target_contract_sha256,
        output_tags=("conflicting-output",),
        retirement_policy="retain",
    ).materialize(work=branch.workflow_plan.work)
    store.create(WorkRecord(work=branch.workflow_plan.work, workflow_plan=conflicting_plan))

    with pytest.raises(ConcurrentWorkUpdate, match="branch child identity was reused"):
        service.admit_branch_set(
            record.work_id,
            decision,
            expected_revision=record.revision,
        )

    parent = store.load(record.work_id)
    assert parent is not None and parent.phase == "planning"
    assert parent.branch_set_plan is None
    selection = decision.selections[0]
    assert store.load_selection(selection.selection_sha256) is None

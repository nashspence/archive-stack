from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Iterator

import pytest
from riverhog_protocol.collection_workflows import (
    ArtifactDisposition,
    CollectionDerivation,
)
from riverhog_protocol.collection_workflows import (
    canonical_json_sha256 as riverhog_canonical_json_sha256,
)
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from stove0_core import (
    ConcurrentWorkUpdate,
    SqlAlchemyStateStore,
    Stove0StateError,
    Stove0WorkService,
    WorkRecord,
    stove0_state_schema,
)
from stove0_protocol import (
    ArtifactSelection,
    ArtifactSubject,
    BranchPlan,
    BranchSetDecision,
    BranchSetPlan,
    BranchSettlement,
    CollectionRootRef,
    JoinDeclaration,
    JoinMemberDeclaration,
    JoinPlan,
    JsonSchemaDocument,
    OperationRef,
    RecipeRef,
    WorkflowPlan,
    WorkflowPlanIntent,
    WorkflowPlanPayload,
    WorkIdentity,
    WorkPayload,
    resolve_join_plan,
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

pytestmark = pytest.mark.integration


@pytest.fixture
def stores() -> Iterator[tuple[SqlAlchemyStateStore, SqlAlchemyStateStore]]:
    database_url = os.environ.get("RIVERHOG_TEST_POSTGRES_URL", "").strip()
    if not database_url:
        pytest.skip("RIVERHOG_TEST_POSTGRES_URL is required")
    schema = "stove0_test_" + uuid.uuid4().hex
    bootstrap = create_engine(database_url)
    with bootstrap.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    bootstrap.dispose()
    scoped_url = str(
        make_url(database_url)
        .update_query_dict({"options": f"-csearch_path={schema}"})
        .render_as_string(hide_password=False)
    )
    assert stove0_state_schema(scoped_url).upgrade().condition == "current"
    assert stove0_state_schema(scoped_url).validate().current_revision == "v1_0002"
    first_engine = create_engine(
        scoped_url,
        pool_pre_ping=True,
    )
    second_engine = create_engine(
        scoped_url,
        pool_pre_ping=True,
    )
    first = SqlAlchemyStateStore(scoped_url, engine=first_engine, initialize=False)
    second = SqlAlchemyStateStore(scoped_url, engine=second_engine, initialize=False)
    try:
        yield first, second
    finally:
        first.engine.dispose()
        second.engine.dispose()
        cleanup = create_engine(database_url)
        with cleanup.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        cleanup.dispose()


def _work() -> WorkIdentity:
    return WorkIdentity.seal(
        WorkPayload(
            recipe=RecipeRef(id="camera.archive/v1", revision=1, sha256="a" * 64),
            inputs=(
                CollectionRootRef(
                    collection_id=1,
                    manifest_sha256="b" * 64,
                    content_etag="c" * 64,
                ),
            ),
        )
    )


def _target_contracts() -> tuple[OperationContract, TargetContract, TransformPlan]:
    operation = OperationContract.seal(
        OperationContractPayload(
            id="fixture.copy/v1",
            intent_schema=JsonSchemaDocument.from_schema(
                "fixture.copy-intent/v1",
                {
                    "type": "object",
                    "properties": {"suffix": {"type": "string"}},
                    "required": ["suffix"],
                    "additionalProperties": False,
                },
            ),
            inputs=(InputArtifactContract(role="fixture.source/v1"),),
            outputs=(
                OutputArtifactContract(
                    role="fixture.output/v1",
                    derived_from_roles=("fixture.source/v1",),
                ),
            ),
        )
    )
    target = TargetContract.seal(
        TargetContractPayload(
            implementation_id="fixture.target/v1",
            implementation_version="1.0.0",
            source_revision="fixture",
            image_digest="9" * 64,
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
    plan = TransformPlan.seal(
        TransformPlanPayload(
            target_implementation_id=target.implementation_id,
            target_contract_sha256=target.contract_sha256,
            operation_id=operation.id,
            operation_contract_sha256=operation.contract_sha256,
            inputs=(
                InputArtifact(
                    id="source",
                    role="fixture.source/v1",
                    collection=_work().inputs[0],
                    path="source/input.bin",
                    bytes=12,
                    sha256="d" * 64,
                ),
            ),
            intent={"suffix": ".copy"},
            target_options={},
        )
    )
    return operation, target, plan


def _active_target_work(
    service: Stove0WorkService,
) -> tuple[WorkRecord, OperationContract, TargetJobStatus, TargetJobStatus]:
    work = _work()
    operation, target, plan = _target_contracts()
    record = service.create_or_resume(work)
    record = service.bind_claim(
        work.work_id,
        claim_id=work.work_id,
        fence=1,
        expected_revision=record.revision,
    )
    record = service.begin_planning(work.work_id, expected_revision=record.revision)
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
    request = TargetJobRequest.seal(
        declaration,
        TargetRuntimeAuthority(
            riverhog_base_url="https://riverhog.invalid",
            capability_token="fixture-secret",
        ),
    )
    record = service.bind_target_request(
        work.work_id,
        request,
        expected_revision=record.revision,
    )
    running = TargetJobStatus(
        job_id=declaration.job_id,
        state="running",
        attempt=1,
        request_sha256=request.request_sha256,
        plan_sha256=plan.plan_sha256,
        progress=TargetProgress(phase="transforming", completed=0),
    )
    record = service.record_target_status(
        work.work_id,
        running,
        operation=operation,
        expected_revision=record.revision,
    )
    canceling = TargetJobStatus(
        job_id=declaration.job_id,
        state="canceling",
        attempt=1,
        request_sha256=request.request_sha256,
        plan_sha256=plan.plan_sha256,
        progress=TargetProgress(phase="canceling", completed=0),
    )
    output = OutputArtifact(
        id="output",
        role="fixture.output/v1",
        path="output/result.bin",
        bytes=12,
        sha256="5" * 64,
        derived_from=("source",),
    )
    derivation = CollectionDerivation(
        execution_id=declaration.job_id,
        claim_id=declaration.claim_id,
        fence=declaration.fence,
        recipe=workflow.work.recipe.to_identity(),
        operation=workflow.operation.to_identity(),
        inputs=tuple(item.to_identity() for item in workflow.work.inputs),
        output_tags=workflow.output_tags,
        execution_envelope_sha256=declaration.job_id,
        execution_sha256="8" * 64,
        controller_evidence=record.controller_evidence.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        ),
        controller_evidence_sha256=riverhog_canonical_json_sha256(
            record.controller_evidence.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
            )
        ),
        dispositions=(
            ArtifactDisposition(
                input_collection_id=work.inputs[0].collection_id,
                input_manifest_sha256=work.inputs[0].manifest_sha256,
                input_path="source/input.bin",
                status="transformed",
                outputs=(output.path,),
            ),
        ),
    )
    output_collection = OutputCollectionRef(
        collection_id=7,
        manifest_sha256="6" * 64,
        content_etag="7" * 64,
        derivation_sha256=derivation.sha256,
    )
    succeeded = TargetJobStatus(
        job_id=declaration.job_id,
        state="succeeded",
        attempt=1,
        request_sha256=request.request_sha256,
        plan_sha256=plan.plan_sha256,
        progress=TargetProgress(phase="done", completed=1, total=1, unit="artifacts"),
        outputs=(output,),
        output_collection=output_collection,
        execution_evidence=TargetExecutionEvidence(
            target_contract_sha256=target.contract_sha256,
            operation_contract_sha256=operation.contract_sha256,
            plan_sha256=plan.plan_sha256,
            execution_sha256=derivation.execution_sha256,
        ),
        derivation=derivation.as_dict(),
    )
    return record, operation, canceling, succeeded


def _branch_decision() -> BranchSetDecision:
    work = _work()
    selection = ArtifactSelection.seal(
        (
            ArtifactSubject(
                id="source",
                role="fixture.source/v1",
                collection=work.inputs[0],
                path="source/input.bin",
                bytes=12,
                sha256="d" * 64,
            ),
        )
    )
    decision_sha256 = "e" * 64
    branches = tuple(
        BranchPlan.build(
            parent_work=work,
            branch_id=branch_id,
            decision_sha256=decision_sha256,
            selection=selection,
            recipe=work.recipe,
            effective_intent={"branch": branch_id},
            workflow_intent=WorkflowPlanIntent(
                operation=OperationRef(id="fixture.branch/v1", sha256="f" * 64),
                target_registration_id="fixture-target",
                target_contract_sha256="1" * 64,
                output_tags=(f"fixture-{branch_id}",),
                retirement_policy="retain",
            ),
        )
        for branch_id in ("audio", "video")
    )
    join = JoinDeclaration.seal(
        members=tuple(
            JoinMemberDeclaration(
                branch_id=branch.branch_id,
                output_roles=("fixture.branch-output/v1",),
            )
            for branch in branches
        ),
        recipe=work.recipe,
        effective_intent={"combine": "exact"},
        workflow_intent=WorkflowPlanIntent(
            operation=OperationRef(id="fixture.join/v1", sha256="2" * 64),
            target_registration_id="fixture-target",
            target_contract_sha256="1" * 64,
            output_tags=("fixture-joined",),
            retirement_policy="retain",
        ),
    )
    documents = {selection.selection_sha256: selection}
    return BranchSetDecision(
        plan=BranchSetPlan.seal(
            parent_work=work,
            decision_sha256=decision_sha256,
            branches=branches,
            join=join,
            selections=documents,
        ),
        selections=(selection,),
    )


def _resolved_join(
    decision: BranchSetDecision,
) -> tuple[JoinPlan, tuple[ArtifactSelection, ...]]:
    documents = dict(decision.selection_documents)
    settlements: list[BranchSettlement] = []
    for offset, branch in enumerate(decision.plan.branches, start=10):
        root = CollectionRootRef(
            collection_id=offset,
            manifest_sha256=f"{offset % 16:x}" * 64,
            content_etag=f"{(offset + 2) % 16:x}" * 64,
        )
        output = ArtifactSelection.seal(
            (
                ArtifactSubject(
                    id=f"{branch.branch_id}-output",
                    role="fixture.branch-output/v1",
                    collection=root,
                    path=f"{branch.branch_id}/output.bin",
                    bytes=12,
                    sha256=f"{(offset + 4) % 16:x}" * 64,
                ),
            )
        )
        documents[output.selection_sha256] = output
        settlements.append(
            BranchSettlement.seal(
                branch=branch,
                derivation_sha256=f"{(offset + 6) % 16:x}" * 64,
                output_collection=root,
                output_selection=output,
            )
        )
    resolved = resolve_join_plan(decision.plan, documents, settlements)
    assert resolved is not None
    return resolved


def test_postgres_concurrent_create_converges_and_controller_worker_cas_is_fenced(
    stores: tuple[SqlAlchemyStateStore, SqlAlchemyStateStore],
) -> None:
    first, second = stores
    work = _work()
    barrier = threading.Barrier(2)
    created: list[object] = []
    failures: list[BaseException] = []

    def create(store: SqlAlchemyStateStore) -> None:
        try:
            barrier.wait(timeout=5)
            created.append(Stove0WorkService(store).create_or_resume(work))
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=create, args=(store,)) for store in stores]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not failures
    assert len(created) == 2 and created[0] == created[1]
    initial = first.load(work.work_id)
    assert initial is not None
    service_a = Stove0WorkService(first)
    service_b = Stove0WorkService(second)
    barrier = threading.Barrier(2)
    winners: list[object] = []
    stale: list[ConcurrentWorkUpdate] = []

    def claim(service: Stove0WorkService) -> None:
        try:
            barrier.wait(timeout=5)
            winners.append(
                service.bind_claim(
                    work.work_id,
                    claim_id=work.work_id,
                    fence=1,
                    expected_revision=initial.revision,
                )
            )
        except ConcurrentWorkUpdate as exc:
            stale.append(exc)

    threads = [
        threading.Thread(target=claim, args=(service,)) for service in (service_a, service_b)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(winners) == 1
    assert len(stale) == 1
    current = first.load(work.work_id)
    assert current is not None and current.phase == "claimed" and current.revision == 2


def test_postgres_concurrent_branch_set_and_join_admission_converge_exactly_once(
    stores: tuple[SqlAlchemyStateStore, SqlAlchemyStateStore],
) -> None:
    first, second = stores
    decision = _branch_decision()
    service = Stove0WorkService(first)
    created = service.create_or_resume(decision.plan.parent_work)
    claimed = service.bind_claim(
        created.work_id,
        claim_id="parent-claim",
        fence=1,
        expected_revision=created.revision,
    )
    planning = service.begin_planning(
        claimed.work_id,
        expected_revision=claimed.revision,
    )

    barrier = threading.Barrier(2)
    admitted: list[object] = []
    failures: list[BaseException] = []

    def admit_branch_set(store: SqlAlchemyStateStore) -> None:
        try:
            barrier.wait(timeout=5)
            admitted.append(
                Stove0WorkService(store).admit_branch_set(
                    planning.work_id,
                    decision,
                    expected_revision=planning.revision,
                )
            )
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=admit_branch_set, args=(store,)) for store in stores]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not failures
    assert len(admitted) == 2 and admitted[0] == admitted[1]
    parent = first.load(planning.work_id)
    assert parent is not None and parent.branch_set_plan == decision.plan
    for branch in decision.plan.branches:
        child = first.load(branch.workflow_plan.work.work_id)
        assert child is not None and child.workflow_plan == branch.workflow_plan

    join_plan, join_selections = _resolved_join(decision)
    barrier = threading.Barrier(2)
    joined: list[object] = []
    failures = []

    def admit_join(store: SqlAlchemyStateStore) -> None:
        try:
            barrier.wait(timeout=5)
            joined.append(
                Stove0WorkService(store).admit_join(
                    parent.work_id,
                    join_plan,
                    join_selections,
                    expected_revision=parent.revision,
                )
            )
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=admit_join, args=(store,)) for store in stores]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not failures
    assert len(joined) == 2 and joined[0] == joined[1]
    parent = second.load(parent.work_id)
    assert parent is not None and parent.join_plan == join_plan
    child = second.load(join_plan.work.work_id)
    assert child is not None and child.workflow_plan == join_plan.workflow_plan


def test_postgres_cancel_completion_race_converges_to_immutable_published_success(
    stores: tuple[SqlAlchemyStateStore, SqlAlchemyStateStore],
) -> None:
    first, second = stores
    record, operation, canceling, succeeded = _active_target_work(Stove0WorkService(first))
    barrier = threading.Barrier(2)
    winners: list[WorkRecord] = []
    stale: list[ConcurrentWorkUpdate] = []

    def settle(store: SqlAlchemyStateStore, status: TargetJobStatus) -> None:
        try:
            barrier.wait(timeout=5)
            winners.append(
                Stove0WorkService(store).record_target_status(
                    record.work_id,
                    status,
                    operation=operation,
                    expected_revision=record.revision,
                )
            )
        except ConcurrentWorkUpdate as exc:
            stale.append(exc)

    threads = [
        threading.Thread(target=settle, args=(first, canceling)),
        threading.Thread(target=settle, args=(second, succeeded)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert len(winners) == 1
    assert len(stale) == 1
    current = first.load(record.work_id)
    assert current is not None
    if current.target_status == canceling:
        current = Stove0WorkService(second).record_target_status(
            record.work_id,
            succeeded,
            operation=operation,
            expected_revision=current.revision,
        )
    assert current.phase == "verifying"
    assert current.target_status == succeeded
    assert current.output == succeeded.output_collection
    with pytest.raises(Stove0StateError, match="terminal target status is immutable"):
        Stove0WorkService(first).record_target_status(
            record.work_id,
            canceling,
            operation=operation,
            expected_revision=current.revision,
        )


def test_postgres_event_cursor_compare_and_swap_prevents_replayed_cursor_regression(
    stores: tuple[SqlAlchemyStateStore, SqlAlchemyStateStore],
) -> None:
    first, second = stores
    assert first.compare_and_swap_cursor("riverhog/v1", expected_revision=None, cursor="10") == (
        "10",
        1,
    )
    assert second.load_cursor("riverhog/v1") == ("10", 1)
    with pytest.raises(ConcurrentWorkUpdate, match="revision is stale"):
        second.compare_and_swap_cursor("riverhog/v1", expected_revision=0, cursor="9")
    assert second.compare_and_swap_cursor("riverhog/v1", expected_revision=1, cursor="11") == (
        "11",
        2,
    )

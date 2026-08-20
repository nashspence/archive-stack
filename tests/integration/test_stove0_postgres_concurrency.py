from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from stove0_core import (
    ConcurrentWorkUpdate,
    SqlAlchemyStateStore,
    Stove0WorkService,
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
    OperationRef,
    RecipeRef,
    WorkflowPlanIntent,
    WorkIdentity,
    WorkPayload,
    resolve_join_plan,
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

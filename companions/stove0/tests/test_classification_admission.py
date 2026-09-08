from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from riverhog_api_client import ApiClient
from riverhog_protocol import (
    CatalogSyncChangePage,
    CatalogSyncCheckpoint,
    CatalogSyncCollectionPage,
    CatalogSyncDelete,
    CatalogSyncDescriptor,
    CatalogSyncUpsert,
)
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from stove0_core import ClassificationAdmissionService, SqlAlchemyStateStore
from stove0_core.persistence import _AdmissionPolicyRow
from stove0_operator_contracts import AdmissionCatalog, AdmissionIntent, AdmissionPolicy
from stove0_protocol import (
    ArtifactSelection,
    ArtifactSubject,
    BranchPlan,
    BranchSetPlan,
    BranchTargetPreview,
    CollectionRootRef,
    OperationRef,
    RecipeRef,
    TargetPlanBinding,
    WorkflowPlanIntent,
    WorkflowPreview,
    WorkflowPreviewPayload,
    WorkflowPreviewRequest,
    WorkflowPreviewRequestPayload,
    WorkIdentity,
    WorkPayload,
)


def _descriptor(
    *,
    tag_revision: int,
    tag_identity: str,
    revision: str,
    collection_id: int = 7,
) -> CatalogSyncDescriptor:
    return CatalogSyncDescriptor(
        collection_id=collection_id,
        archive_root_sha256="1" * 64,
        content_identity="2" * 64,
        description=None,
        description_revision=0,
        description_identity="3" * 64,
        tag_revision=tag_revision,
        tag_set_identity=tag_identity,
        revision=revision,
    )


class _CatalogApi:
    def __init__(self, descriptor: CatalogSyncDescriptor, tags: set[str]) -> None:
        self.descriptor = descriptor
        self.tags_by_identity = {descriptor.tag_set_identity: tags}
        self.change: CatalogSyncUpsert | None = None
        self.source_identity = "4" * 64
        self.view_identity = "5" * 64
        self.membership_calls: list[tuple[int, str, int, str]] = []

    def create_catalog_sync_checkpoint(self) -> CatalogSyncCheckpoint:
        return CatalogSyncCheckpoint(
            source_identity=self.source_identity,
            authorization_view_identity=self.view_identity,
            catalog_cursor="baseline",
        )

    def list_catalog_sync_collections(
        self, cursor: str, *, limit: int
    ) -> CatalogSyncCollectionPage:
        assert cursor == "baseline"
        assert limit == 100
        return CatalogSyncCollectionPage(
            source_identity=self.source_identity,
            authorization_view_identity=self.view_identity,
            collections=[self.descriptor],
            changes_cursor="following",
        )

    def list_catalog_sync_changes(self, cursor: str, *, limit: int) -> CatalogSyncChangePage:
        assert cursor == "following"
        assert limit == 1
        return CatalogSyncChangePage(
            source_identity=self.source_identity,
            authorization_view_identity=self.view_identity,
            changes=[] if self.change is None else [self.change],
            next_cursor="following",
            caught_up=True,
            through_revision=self.change.revision if self.change is not None else "1",
        )

    def collection_contains_tag(
        self,
        collection_id: int,
        *,
        tag: str,
        revision: int,
        tag_set_identity: str,
    ) -> dict[str, object]:
        self.membership_calls.append((collection_id, tag, revision, tag_set_identity))
        return {
            "collection_id": collection_id,
            "tag": tag,
            "revision": revision,
            "tag_set_identity": tag_set_identity,
            "present": tag in self.tags_by_identity[tag_set_identity],
        }


class _Planner:
    def __init__(self, policy: AdmissionPolicy) -> None:
        self.policy = policy
        self.catalog = SimpleNamespace(recipe=self._recipe)

    def _recipe(self, recipe_id: str, revision: int) -> SimpleNamespace:
        assert (recipe_id, revision) == (self.policy.recipe_id, self.policy.recipe_revision)
        return SimpleNamespace(sha256=self.policy.recipe_sha256)

    def create_work(
        self,
        recipe_id: str,
        roots: tuple[CollectionRootRef, ...],
        *,
        revision: int,
        effective_intent: dict[str, object],
    ) -> WorkIdentity:
        return WorkIdentity.seal(
            WorkPayload(
                recipe=RecipeRef(
                    id=recipe_id,
                    revision=revision,
                    sha256=self.policy.recipe_sha256,
                ),
                inputs=roots,
                effective_intent=effective_intent,
            )
        )


def _policy(*, policy_id: str = "camera-archive") -> AdmissionPolicy:
    return AdmissionPolicy(
        id=policy_id,
        revision=1,
        required_tags=("camera", "workflow/archive"),
        recipe_id="stove0.media.archive/v1",
        recipe_revision=1,
        recipe_sha256="a" * 64,
        effective_intent={"quality": "archive"},
    )


def _state() -> SqlAlchemyStateStore:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    return SqlAlchemyStateStore("sqlite+pysqlite:///:memory:", engine=engine)


def _service(
    *,
    state: SqlAlchemyStateStore,
    api: _CatalogApi,
    policy: AdmissionPolicy,
    preview: object | None = None,
    coordinator: object | None = None,
) -> ClassificationAdmissionService:
    return ClassificationAdmissionService(
        catalog=AdmissionCatalog(policies=(policy,)),
        riverhog=cast(ApiClient, api),
        state=state,
        planner=cast(Any, _Planner(policy)),
        preview=cast(Any, preview if preview is not None else object()),
        coordinator=cast(Any, coordinator if coordinator is not None else object()),
    )


def _ready_preview(work: WorkIdentity) -> WorkflowPreview:
    request = WorkflowPreviewRequest.seal(WorkflowPreviewRequestPayload(work=work))
    selection = ArtifactSelection.seal(
        (
            ArtifactSubject(
                id="source",
                role="fixture.source/v1",
                collection=work.inputs[0],
                path="source/input.bin",
                bytes=12,
                sha256="b" * 64,
                media_type="application/octet-stream",
            ),
        )
    )
    operation = OperationRef(id="fixture.archive/v1", sha256="c" * 64)
    branch = BranchPlan.build(
        parent_work=work,
        branch_id="archive",
        decision_sha256="d" * 64,
        selection=selection,
        recipe=work.recipe,
        effective_intent=work.effective_intent,
        workflow_intent=WorkflowPlanIntent(
            operation=operation,
            target_registration_id="fixture-target",
            target_contract_sha256="e" * 64,
            retirement_policy="retain",
        ),
    )
    branch_set = BranchSetPlan.seal(
        parent_work=work,
        decision_sha256="d" * 64,
        branches=(branch,),
        selections={selection.selection_sha256: selection},
    )
    workflow = branch.workflow_plan
    return WorkflowPreview.seal(
        WorkflowPreviewPayload(
            preview_id=request.preview_id,
            state="ready",
            work=work,
            branch_set_plan=branch_set,
            selections=(selection,),
            target_plans=(
                BranchTargetPreview(
                    branch_id=branch.branch_id,
                    work_id=workflow.work.work_id,
                    workflow_plan_sha256=workflow.workflow_plan_sha256,
                    target_plan=TargetPlanBinding(
                        protocol="stove0-transform-target/v1",
                        target_implementation_id="fixture.target/v1",
                        target_contract_sha256=workflow.target_contract_sha256,
                        operation_contract_sha256=operation.sha256,
                        plan={"format": "fixture-target-plan/v1"},
                        plan_sha256="f" * 64,
                    ),
                ),
            ),
        )
    )


class _Preview:
    def preview(self, work: WorkIdentity) -> WorkflowPreview:
        return _ready_preview(work)


class _SelectivePreview:
    def __init__(self, failing_collection_id: int) -> None:
        self.failing_collection_id = failing_collection_id

    def preview(self, work: WorkIdentity) -> WorkflowPreview:
        if work.inputs[0].collection_id == self.failing_collection_id:
            raise RuntimeError("permanent candidate failure")
        return _ready_preview(work)


class _Coordinator:
    def __init__(self) -> None:
        self.calls: list[tuple[WorkIdentity, WorkflowPreview]] = []

    def create_or_resume(self, work: WorkIdentity, *, preview: WorkflowPreview) -> SimpleNamespace:
        self.calls.append((work, preview))
        return SimpleNamespace(work=work)


def test_admission_baseline_is_non_triggering_and_false_to_true_is_exactly_once() -> None:
    state = _state()
    initial = _descriptor(tag_revision=1, tag_identity="6" * 64, revision="1")
    api = _CatalogApi(initial, {"camera"})
    policy = _policy()
    service = _service(state=state, api=api, policy=policy)

    assert service.advance(limit=1).failures == ()
    assert service.advance(limit=1).failures == ()
    assert (
        service.list_admissions(
            page_size=25,
            position=None,
            policy_id=None,
            state=None,
            query=None,
            sort="created_at",
            order="desc",
        )["admissions"]
        == ()
    )

    changed = _descriptor(tag_revision=2, tag_identity="7" * 64, revision="2")
    api.tags_by_identity[changed.tag_set_identity] = {"camera", "workflow/archive"}
    api.change = CatalogSyncUpsert(**changed.model_dump())
    assert service.advance(limit=1).failures == ()

    admissions = service.list_admissions(
        page_size=25,
        position=None,
        policy_id=policy.id,
        state="intent",
        query="camera",
        sort="admission_id",
        order="asc",
    )["admissions"]
    assert len(cast(tuple[object, ...], admissions)) == 1
    assert api.membership_calls == [
        (7, "camera", 1, "6" * 64),
        (7, "workflow/archive", 1, "6" * 64),
        (7, "camera", 2, "7" * 64),
        (7, "workflow/archive", 2, "7" * 64),
    ]

    restarted = _service(state=state, api=api, policy=policy)
    assert (
        len(
            cast(
                tuple[object, ...],
                restarted.list_admissions(
                    page_size=25,
                    position=None,
                    policy_id=None,
                    state=None,
                    query=None,
                    sort="created_at",
                    order="desc",
                )["admissions"],
            )
        )
        == 1
    )


def test_still_matching_update_does_not_create_implicit_work() -> None:
    state = _state()
    initial = _descriptor(tag_revision=1, tag_identity="6" * 64, revision="1")
    api = _CatalogApi(initial, {"camera", "workflow/archive"})
    policy = _policy()
    service = _service(state=state, api=api, policy=policy)
    service.advance(limit=1)
    service.advance(limit=1)

    changed = _descriptor(tag_revision=2, tag_identity="7" * 64, revision="2")
    api.tags_by_identity[changed.tag_set_identity] = {"camera", "workflow/archive"}
    api.change = CatalogSyncUpsert(**changed.model_dump())
    service.advance(limit=1)

    assert (
        service.list_admissions(
            page_size=25,
            position=None,
            policy_id=None,
            state=None,
            query=None,
            sort="created_at",
            order="desc",
        )["admissions"]
        == ()
    )


def test_stale_upsert_cannot_resurrect_a_deleted_catalog_revision() -> None:
    state = _state()
    initial = _descriptor(tag_revision=1, tag_identity="6" * 64, revision="1")
    api = _CatalogApi(initial, {"camera", "workflow/archive"})
    policy = _policy()
    service = _service(state=state, api=api, policy=policy)
    service.advance(limit=1)
    service.advance(limit=1)

    deleted = CatalogSyncDelete(collection_id=7, revision="3")
    delete_page = CatalogSyncChangePage(
        source_identity=api.source_identity,
        authorization_view_identity=api.view_identity,
        changes=[deleted],
        next_cursor="after-delete",
        caught_up=False,
        through_revision="3",
    )
    assert service._commit_change_page(  # noqa: SLF001 - exact replay regression proof
        policy,
        cursor="following",
        page=delete_page,
        evaluated=None,
    )

    restarted = _service(state=state, api=api, policy=policy)
    stale_descriptor = _descriptor(
        tag_revision=2,
        tag_identity="7" * 64,
        revision="2",
    )
    api.tags_by_identity[stale_descriptor.tag_set_identity] = {
        "camera",
        "workflow/archive",
    }
    stale = CatalogSyncUpsert(**stale_descriptor.model_dump())
    stale_page = CatalogSyncChangePage(
        source_identity=api.source_identity,
        authorization_view_identity=api.view_identity,
        changes=[stale],
        next_cursor="after-stale",
        caught_up=True,
        through_revision="3",
    )
    assert restarted._commit_change_page(  # noqa: SLF001 - exact replay regression proof
        policy,
        cursor="after-delete",
        page=stale_page,
        evaluated=(stale, True),
    )

    assert (
        restarted.list_admissions(
            page_size=25,
            position=None,
            policy_id=None,
            state=None,
            query=None,
            sort="created_at",
            order="desc",
        )["admissions"]
        == ()
    )


def test_equal_catalog_revision_with_different_authority_fails_closed() -> None:
    state = _state()
    initial = _descriptor(tag_revision=1, tag_identity="6" * 64, revision="1")
    api = _CatalogApi(initial, {"camera"})
    policy = _policy()
    service = _service(state=state, api=api, policy=policy)
    service.advance(limit=1)
    service.advance(limit=1)

    conflicting_descriptor = _descriptor(
        tag_revision=2,
        tag_identity="7" * 64,
        revision="1",
    )
    conflict = CatalogSyncUpsert(**conflicting_descriptor.model_dump())
    page = CatalogSyncChangePage(
        source_identity=api.source_identity,
        authorization_view_identity=api.view_identity,
        changes=[conflict],
        next_cursor="after-conflict",
        caught_up=True,
        through_revision="1",
    )

    with pytest.raises(RuntimeError, match="changed its exact authority"):
        service._commit_change_page(  # noqa: SLF001 - exact replay regression proof
            policy,
            cursor="following",
            page=page,
            evaluated=(conflict, False),
        )
    assert service.policies().policies[0].through_revision == "0"


def test_failed_lowest_candidate_is_delayed_and_does_not_starve_the_next() -> None:
    state = _state()
    initial = _descriptor(tag_revision=1, tag_identity="6" * 64, revision="1")
    api = _CatalogApi(initial, {"camera", "workflow/archive"})
    policy = _policy()
    descriptors = (
        initial,
        _descriptor(
            collection_id=8,
            tag_revision=1,
            tag_identity="7" * 64,
            revision="2",
        ),
    )
    intents = tuple(AdmissionIntent.seal(policy=policy, collection=item) for item in descriptors)
    failing = min(intents, key=lambda item: item.admission_id)
    succeeding = max(intents, key=lambda item: item.admission_id)
    service = _service(
        state=state,
        api=api,
        policy=policy,
        preview=_SelectivePreview(failing.collection.collection_id),
    )
    with state.sessions() as session, session.begin():
        policy_row = session.get(_AdmissionPolicyRow, policy.id)
        assert policy_row is not None
        policy_row.phase = "reset_required"
        for descriptor in descriptors:
            service._record_intent(session, policy, descriptor)  # noqa: SLF001

    first = service.advance(limit=1)
    assert [failure.event_id for failure in first.failures] == [f"admission:{failing.admission_id}"]
    delayed = service.get_admission(failing.admission_id)
    assert delayed.attempt_count == 1
    assert delayed.next_attempt_at is not None
    assert delayed.failure == "RuntimeError: permanent candidate failure"

    restarted = _service(
        state=state,
        api=api,
        policy=policy,
        preview=_SelectivePreview(failing.collection.collection_id),
    )
    assert restarted.advance(limit=1).failures == ()
    assert restarted.get_admission(succeeding.admission_id).state == "previewed"
    assert restarted.get_admission(failing.admission_id).attempt_count == 1


def test_explicit_backfill_advances_through_restart_safe_preview_and_work_binding() -> None:
    state = _state()
    descriptor = _descriptor(tag_revision=1, tag_identity="6" * 64, revision="1")
    api = _CatalogApi(descriptor, {"camera", "workflow/archive"})
    policy = _policy()
    coordinator = _Coordinator()
    service = _service(
        state=state,
        api=api,
        policy=policy,
        preview=_Preview(),
        coordinator=coordinator,
    )
    assert service.rebaseline(policy.id, mode="backfill").baseline_mode == "backfill"
    service.advance(limit=1)
    intent = cast(
        tuple[Any, ...],
        service.list_admissions(
            page_size=25,
            position=None,
            policy_id=None,
            state=None,
            query=None,
            sort="created_at",
            order="desc",
        )["admissions"],
    )[0]
    assert intent.state == "intent"

    restarted = _service(
        state=state,
        api=api,
        policy=policy,
        preview=_Preview(),
        coordinator=coordinator,
    )
    restarted.advance(limit=1)
    assert restarted.get_admission(intent.intent.admission_id).state == "previewed"

    restarted_again = _service(
        state=state,
        api=api,
        policy=policy,
        preview=_Preview(),
        coordinator=coordinator,
    )
    restarted_again.advance(limit=1)
    bound = restarted_again.get_admission(intent.intent.admission_id)
    assert bound.state == "work_bound"
    assert bound.preview_sha256 is not None
    assert bound.work_id == coordinator.calls[0][0].work_id


def test_catalog_authority_change_persists_explicit_rebaseline_requirement() -> None:
    state = _state()
    initial = _descriptor(tag_revision=1, tag_identity="6" * 64, revision="1")
    api = _CatalogApi(initial, {"camera"})
    policy = _policy()
    service = _service(state=state, api=api, policy=policy)
    service.advance(limit=1)
    service.advance(limit=1)

    api.view_identity = "8" * 64
    run = service.advance(limit=1)

    assert len(run.failures) == 1
    assert service.policies().policies[0].phase == "reset_required"


def test_policy_identity_is_evidence_but_semantic_work_converges() -> None:
    first = _policy(policy_id="camera-a")
    second = _policy(policy_id="camera-b")
    descriptor = _descriptor(tag_revision=1, tag_identity="6" * 64, revision="1")
    first_intent = first.effective_intent
    second_intent = second.effective_intent

    first_work = _Planner(first).create_work(
        first.recipe_id,
        (
            CollectionRootRef(
                collection_id=descriptor.collection_id,
                archive_root_sha256=descriptor.archive_root_sha256,
                content_identity=descriptor.content_identity,
            ),
        ),
        revision=first.recipe_revision,
        effective_intent=first_intent,
    )
    second_work = _Planner(second).create_work(
        second.recipe_id,
        first_work.inputs,
        revision=second.recipe_revision,
        effective_intent=second_intent,
    )

    assert first.policy_sha256 != second.policy_sha256
    assert first_work.work_id == second_work.work_id


def test_committed_admission_survives_later_policy_edit() -> None:
    state = _state()
    descriptor = _descriptor(tag_revision=1, tag_identity="6" * 64, revision="1")
    api = _CatalogApi(descriptor, {"camera", "workflow/archive"})
    original = _policy()
    initial = _service(state=state, api=api, policy=original)
    initial.rebaseline(original.id, mode="backfill")
    initial.advance(limit=1)

    edited = AdmissionPolicy(
        id=original.id,
        revision=2,
        required_tags=("camera",),
        recipe_id=original.recipe_id,
        recipe_revision=original.recipe_revision,
        recipe_sha256=original.recipe_sha256,
        effective_intent={"quality": "different-future-work"},
    )
    coordinator = _Coordinator()
    service = _service(
        state=state,
        api=api,
        policy=edited,
        preview=_Preview(),
        coordinator=coordinator,
    )

    assert service.policies().policies[0].phase == "reset_required"
    service.advance(limit=1)
    service.advance(limit=1)

    admission = cast(
        tuple[Any, ...],
        service.list_admissions(
            page_size=25,
            position=None,
            policy_id=original.id,
            state="work_bound",
            query=None,
            sort="admission_id",
            order="asc",
        )["admissions"],
    )[0]
    assert admission.intent.policy_revision == 1
    assert admission.intent.effective_intent == {"quality": "archive"}
    assert coordinator.calls[0][0].effective_intent == {"quality": "archive"}

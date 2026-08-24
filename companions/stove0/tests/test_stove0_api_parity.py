from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast, get_type_hints

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from riverhog_api_client import ApiClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from stove0_api.app import Stove0Composition, _log_scheduler_failures, create_app
from stove0_api_client import Stove0ApiClient, Stove0ApiError
from stove0_cli import main as stove0_cli
from stove0_core import (
    ClaimBinding,
    EvaluationChild,
    EvaluationRecord,
    EvaluationService,
    PreviewAcceptance,
    RecipeCatalog,
    SqlAlchemyStateStore,
    Stove0Coordinator,
    Stove0RuntimeConfig,
    Stove0Scheduler,
    Stove0WorkService,
    WorkflowPreviewService,
    WorkRecord,
)
from stove0_operator_contracts import (
    EvaluationReviewIn,
    SchedulerRunIn,
    Stove0EventPage,
    WorkCancelIn,
    WorkCreateIn,
    WorkflowPreviewIn,
)
from stove0_protocol import (
    ArtifactSelection,
    ArtifactSubject,
    BranchPlan,
    BranchSetEvaluation,
    BranchSetPlan,
    BranchTargetPreview,
    CollectionRootRef,
    EvaluationDefinition,
    EvaluationDefinitionPayload,
    EvaluationMatrix,
    EvaluationMatrixPayload,
    EvaluationVariant,
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
from typer.testing import CliRunner

from tests.operation_observer import OperationObserver, TimeoutNeutralTestClient


class CatalogApi:
    def close(self) -> None:
        pass


class _LifecycleCatalogApi(CatalogApi):
    def get_collection(self, collection_id: int) -> dict[str, object]:
        return {
            "id": collection_id,
            "archive_root_sha256": "1" * 64,
            "content_identity": "2" * 64,
        }


class _LifecycleState:
    def __init__(self) -> None:
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.engine = engine
        self.work_record: WorkRecord | None = None
        self.evaluation_record = _evaluation_record(_evaluation_definition())

    def list_events(self, **_kwargs: object) -> Stove0EventPage:
        return Stove0EventPage(events=[], next_cursor="0", has_more=False)

    def list_work(self, **_kwargs: object) -> dict[str, object]:
        records = () if self.work_record is None else (self.work_record,)
        return {
            "page": 1,
            "per_page": 25,
            "total": len(records),
            "pages": 1 if records else 0,
            "sort": "updated_at",
            "order": "desc",
            "filters": {},
            "work": records,
        }

    def load(self, work_id: str) -> WorkRecord | None:
        if self.work_record is not None and self.work_record.work_id == work_id:
            return self.work_record
        return None

    def list_evaluations(self, **_kwargs: object) -> dict[str, object]:
        return {
            "page": 1,
            "per_page": 25,
            "total": 1,
            "pages": 1,
            "sort": "updated_at",
            "order": "desc",
            "filters": {},
            "evaluations": [self.evaluation_record],
        }

    def load_evaluation(self, evaluation_id: str) -> EvaluationRecord | None:
        if self.evaluation_record.evaluation_id == evaluation_id:
            return self.evaluation_record
        return None

    def load_selection(self, selection_sha256: str) -> ArtifactSelection | None:
        selection = _fixture_selection(_fixture_work())
        return selection if selection.selection_sha256 == selection_sha256 else None

    def load_cursor(self, _stream: str) -> None:
        return None


class _LifecyclePlanner:
    def create_work(
        self,
        recipe_id: str,
        roots: list[CollectionRootRef],
        *,
        revision: int | None = None,
        effective_intent: dict[str, object] | None = None,
    ) -> WorkIdentity:
        return WorkIdentity.seal(
            WorkPayload(
                recipe=RecipeRef(
                    id=recipe_id,
                    revision=revision or 1,
                    sha256="3" * 64,
                ),
                inputs=tuple(roots),
                effective_intent=effective_intent or {},
            )
        )


class _LifecycleCoordinator:
    planning = _LifecyclePlanner()

    def __init__(self, state: _LifecycleState) -> None:
        self.state = state

    def create_or_resume(self, identity: WorkIdentity, *, preview: WorkflowPreview) -> WorkRecord:
        self.state.work_record = WorkRecord(
            work=identity,
            preview_acceptance=PreviewAcceptance.from_preview(preview),
        )
        return self.state.work_record

    def step(self, work_id: str) -> WorkRecord:
        current = self._load(work_id)
        self.state.work_record = WorkRecord(
            work=current.work,
            phase="claimed",
            revision=current.revision + 1,
            claim=ClaimBinding(claim_id="qualification", fence=1),
            preview_acceptance=current.preview_acceptance,
        )
        return self.state.work_record

    def retry(self, work_id: str) -> WorkRecord:
        current = self._load(work_id)
        self.state.work_record = WorkRecord(
            work=current.work,
            phase="eligible",
            revision=current.revision + 1,
            preview_acceptance=current.preview_acceptance,
        )
        return self.state.work_record

    def cancel(self, work_id: str, *, reason: str | None) -> WorkRecord:
        del reason
        current = self._load(work_id)
        self.state.work_record = WorkRecord(
            work=current.work,
            phase="canceled",
            revision=current.revision + 1,
            preview_acceptance=current.preview_acceptance,
        )
        return self.state.work_record

    def inspect_coordination(self, work_id: str) -> BranchSetEvaluation:
        preview = _ready_preview(self._load(work_id).work)
        assert preview.branch_set_plan is not None
        return BranchSetEvaluation(
            branch_set_sha256=preview.branch_set_plan.branch_set_sha256,
            succeeded_branches=(),
            succeeded_effects=(),
            succeeded_coordinations=(),
            unsettled_branch_ids=("archive",),
            failed_branch_ids=(),
            inapplicable_branch_ids=(),
            interrupted_branch_ids=(),
            canceled_branch_ids=(),
            join_ready=False,
            resolved_join_plan=None,
            join_state="not-declared",
            join_settlement=None,
            unsettled_work_ids=(preview.branch_set_plan.branches[0].workflow_plan.work.work_id,),
            branch_set_succeeded=False,
            coordination_settlement=None,
            retirement_requested=False,
            coordination_complete_for_retirement=False,
        )

    def _load(self, work_id: str) -> WorkRecord:
        record = self.state.load(work_id)
        if record is None:
            raise KeyError(work_id)
        return record


class _LifecyclePreview:
    def preview(self, identity: object) -> WorkflowPreview:
        return _ready_preview(WorkIdentity.model_validate(identity))


class _LifecycleEvaluations:
    def __init__(self, state: _LifecycleState) -> None:
        self.state = state

    def create_or_resume(self, definition: EvaluationDefinition) -> EvaluationRecord:
        self.state.evaluation_record = _evaluation_record(definition)
        return self.state.evaluation_record

    def refresh(self, evaluation_id: str) -> EvaluationRecord:
        return self._load(evaluation_id)

    def step(self, evaluation_id: str, **_kwargs: object) -> EvaluationRecord:
        return self._load(evaluation_id)

    def cancel(self, evaluation_id: str, **_kwargs: object) -> EvaluationRecord:
        current = self._load(evaluation_id)
        self.state.evaluation_record = current.model_copy(
            update={
                "phase": "canceled",
                "revision": current.revision + 1,
                "children": tuple(
                    child.model_copy(update={"state": "canceled"}) for child in current.children
                ),
            }
        )
        return self.state.evaluation_record

    def retry_failed(
        self, evaluation_id: str, _variant_id: str, **_kwargs: object
    ) -> EvaluationRecord:
        current = self._load(evaluation_id)
        self.state.evaluation_record = _evaluation_record(
            current.definition,
            revision=current.revision + 1,
        )
        return self.state.evaluation_record

    def review(self, evaluation_id: str, review: object) -> EvaluationRecord:
        current = self._load(evaluation_id)
        self.state.evaluation_record = current.model_copy(
            update={"revision": current.revision + 1, "reviews": (review,)}
        )
        return self.state.evaluation_record

    def _load(self, evaluation_id: str) -> EvaluationRecord:
        record = self.state.load_evaluation(evaluation_id)
        if record is None:
            raise KeyError(evaluation_id)
        return record


class _LifecycleScheduler:
    def run_once(self, **_kwargs: object) -> dict[str, object]:
        return {
            "pruning": None,
            "events": {
                "events": 0,
                "next_cursor": "0",
                "has_more": False,
                "work_ids": [],
                "failures": [],
            },
            "work": {
                "role": "combined",
                "cursor": "",
                "next_cursor": "",
                "progressed": [],
                "failures": [],
            },
        }


def _lifecycle_composition() -> Stove0Composition:
    state = _LifecycleState()
    fixture_path = Path(__file__).parents[3] / "qualification/fixtures/stove0/recipes.yaml"
    return Stove0Composition(
        config=Stove0RuntimeConfig(
            database_url="sqlite+pysqlite:///:memory:",
            api_token="stove0-test-token",
            riverhog_base_url="https://riverhog.invalid",
            riverhog_token="riverhog-test-token",
            riverhog_allow_insecure_http=False,
            recipes_path=Path("recipes.yaml"),
            observers={},
            targets={},
            workspace_assurance="ephemeral",
            claim_lease_seconds=1800,
            capability_ttl_seconds=900,
            scheduler_interval_seconds=5,
            operational_state_retention_seconds=2592000,
        ),
        riverhog_api=cast(ApiClient, _LifecycleCatalogApi()),
        state=cast(SqlAlchemyStateStore, state),
        recipes=RecipeCatalog.load(fixture_path),
        work=cast(Stove0WorkService, object()),
        coordinator=cast(Stove0Coordinator, _LifecycleCoordinator(state)),
        preview=cast(WorkflowPreviewService, _LifecyclePreview()),
        evaluations=cast(EvaluationService, _LifecycleEvaluations(state)),
        scheduler=cast(Stove0Scheduler, _LifecycleScheduler()),
    )


def _evaluation_definition() -> EvaluationDefinition:
    matrix = EvaluationMatrix.seal(
        EvaluationMatrixPayload(variants=(EvaluationVariant(id="variant-a"),))
    )
    return EvaluationDefinition.seal(
        EvaluationDefinitionPayload(
            purpose="trial",
            recipe=RecipeRef(id="fixture.recipe/v1", revision=1, sha256="3" * 64),
            inputs=(
                CollectionRootRef(
                    collection_id=1,
                    archive_root_sha256="1" * 64,
                    content_identity="2" * 64,
                ),
            ),
            matrix=matrix,
        )
    )


def _evaluation_record(
    definition: EvaluationDefinition,
    *,
    revision: int = 1,
) -> EvaluationRecord:
    return EvaluationRecord(
        definition=definition,
        phase="running",
        revision=revision,
        children=tuple(
            EvaluationChild(
                variant_id=variant.id,
                work_id=definition.child_work(variant.id).work_id,
            )
            for variant in definition.matrix.variants
        ),
    )


def _fixture_work(recipe_id: str = "stove0.conformance-media/v1") -> WorkIdentity:
    return WorkIdentity.seal(
        WorkPayload(
            recipe=RecipeRef(id=recipe_id, revision=1, sha256="3" * 64),
            inputs=(
                CollectionRootRef(
                    collection_id=1,
                    archive_root_sha256="1" * 64,
                    content_identity="2" * 64,
                ),
            ),
        )
    )


def _fixture_selection(work: WorkIdentity) -> ArtifactSelection:
    return ArtifactSelection.seal(
        (
            ArtifactSubject(
                id="source",
                role="fixture.source/v1",
                collection=work.inputs[0],
                path="source/input.bin",
                bytes=12,
                sha256="4" * 64,
                media_type="application/octet-stream",
            ),
        )
    )


def _ready_preview(work: WorkIdentity) -> WorkflowPreview:
    request = WorkflowPreviewRequest.seal(WorkflowPreviewRequestPayload(work=work))
    selection = _fixture_selection(work)
    operation = OperationRef(id="fixture.copy/v1", sha256="5" * 64)
    branch = BranchPlan.build(
        parent_work=work,
        branch_id="archive",
        decision_sha256="6" * 64,
        selection=selection,
        recipe=work.recipe,
        effective_intent=work.effective_intent,
        workflow_intent=WorkflowPlanIntent(
            operation=operation,
            target_registration_id="fixture-target",
            target_contract_sha256="7" * 64,
            output_tags=("fixture-output",),
            retirement_policy="retain",
        ),
    )
    branch_set = BranchSetPlan.seal(
        parent_work=work,
        decision_sha256="6" * 64,
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
                        plan_sha256="8" * 64,
                    ),
                ),
            ),
        )
    )


def _composition() -> Stove0Composition:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    state = SqlAlchemyStateStore("sqlite+pysqlite:///:memory:", engine=engine)
    work = Stove0WorkService(state)
    return Stove0Composition(
        config=Stove0RuntimeConfig(
            database_url="sqlite+pysqlite:///:memory:",
            api_token="stove0-test-token",
            riverhog_base_url="https://riverhog.invalid",
            riverhog_token="riverhog-test-token",
            riverhog_allow_insecure_http=False,
            recipes_path=Path("recipes.yaml"),
            observers={},
            targets={},
            workspace_assurance="ephemeral",
            claim_lease_seconds=1800,
            capability_ttl_seconds=900,
            scheduler_interval_seconds=5,
            operational_state_retention_seconds=2592000,
        ),
        riverhog_api=cast(ApiClient, CatalogApi()),
        state=state,
        recipes=RecipeCatalog(operations=(), recipes=()),
        work=work,
        coordinator=cast(Stove0Coordinator, object()),
        preview=cast(WorkflowPreviewService, object()),
        evaluations=EvaluationService(state.evaluation_store(), work=work),
        scheduler=cast(Stove0Scheduler, object()),
    )


def _operations() -> dict[str, str]:
    schema = create_app(_composition()).openapi()
    return {
        str(operation["operationId"]): f"{method.upper()} {path}"
        for path, path_item in schema["paths"].items()
        if path.startswith("/v1")
        for method, operation in path_item.items()
        if method in {"delete", "get", "patch", "post", "put"}
    }


def test_stove0_official_client_positive_disposable_lifecycle() -> None:
    application = create_app(_lifecycle_composition())
    observer = OperationObserver.install(application, application="stove0")
    definition = _evaluation_definition()
    evaluation_id = definition.evaluation_id

    with TestClient(application) as transport:
        client = Stove0ApiClient(
            "http://testserver",
            "stove0-test-token",
            allow_insecure_http=True,
        )
        client._client = cast(  # type: ignore[assignment]
            Any,
            TimeoutNeutralTestClient(transport, observer=observer),
        )

        assert client.health_live().status == "ok"
        assert client.health_ready().status == "ok"
        assert client.list_events().next_cursor == "0"
        recipe_page = client.list_recipes()
        assert recipe_page.catalog_sha256
        assert recipe_page.recipes[0].sha256
        assert client.get_recipe("stove0.conformance-media/v1").sha256
        listed = client.list_work(all_items=True)
        assert listed.total == 0
        preview = client.preview_workflow("stove0.conformance-media/v1", [1])
        created = client.create_work(
            "stove0.conformance-media/v1",
            [1],
            preview_sha256=preview.preview_sha256,
        )
        assert created.work_id == _fixture_work().work_id
        fetched = client.get_work(created.work_id)
        assert fetched.work_id == created.work_id
        assert client.inspect_work_coordination(created.work_id).branch_set_succeeded is False
        selection = _fixture_selection(_fixture_work())
        assert client.get_artifact_selection(selection.selection_sha256).total == 1
        assert client.step_work(created.work_id).phase == "claimed"
        assert client.retry_work(created.work_id).phase == "eligible"
        assert client.cancel_work(created.work_id, reason="qualification").phase == "canceled"
        assert preview.state == "ready"
        assert client.list_evaluations(all_items=True).total == 1
        assert (
            client.create_evaluation(definition.model_dump(mode="json")).evaluation_id
            == evaluation_id
        )
        assert client.get_evaluation(evaluation_id).evaluation_id == evaluation_id
        assert client.step_evaluation(evaluation_id).evaluation_id == evaluation_id
        assert client.cancel_evaluation(evaluation_id, reason="qualification").phase == "canceled"
        assert (
            client.retry_evaluation_variant(evaluation_id, "variant-a").evaluation_id
            == evaluation_id
        )
        assert (
            client.review_evaluation_variant(
                evaluation_id,
                "variant-a",
                rating=5,
            ).evaluation_id
            == evaluation_id
        )
        assert client.scheduler_status().roles == ("controller", "worker", "combined")
        assert client.run_scheduler(role="combined").work.role == "combined"
        client._client = None

    observer.require(_operations())


def test_every_stove0_api_operation_has_one_current_official_client_method() -> None:
    operations = _operations()
    assert len(operations) == 21
    assert {
        operation_id
        for operation_id in operations
        if not callable(getattr(Stove0ApiClient, operation_id, None))
    } == set()
    public_methods = {
        name
        for name in dir(Stove0ApiClient)
        if not name.startswith("_") and callable(getattr(Stove0ApiClient, name))
    }
    assert public_methods - set(operations) == {"close", "health_live", "health_ready"}
    assert {
        operation_id
        for operation_id in operations
        if get_type_hints(getattr(Stove0ApiClient, operation_id))["return"] in {Any, dict}
    } == set()


def test_every_stove0_operation_publishes_an_exact_response_schema() -> None:
    schema = create_app(_composition()).openapi()
    for path, path_item in schema["paths"].items():
        if not path.startswith("/v1"):
            continue
        for method, operation in path_item.items():
            if method not in {"delete", "get", "patch", "post", "put"}:
                continue
            success = next(
                response
                for status, response in operation["responses"].items()
                if 200 <= int(status) < 300
            )
            response_schema = success["content"]["application/json"]["schema"]
            reference = response_schema.get("$ref")
            assert isinstance(reference, str), (method, path, response_schema)
            component = schema["components"]["schemas"][reference.rsplit("/", 1)[-1]]
            assert component.get("additionalProperties") is False, (method, path, component)
            request_body = operation.get("requestBody")
            if request_body is not None:
                request_schema = request_body["content"]["application/json"]["schema"]
                assert isinstance(request_schema.get("$ref"), str), (
                    method,
                    path,
                    request_schema,
                )


def test_stove0_request_bodies_have_one_shared_public_contract_owner() -> None:
    expected = {
        "create_work": ("request", WorkCreateIn),
        "cancel_work": ("request", WorkCancelIn),
        "preview_workflow": ("request", WorkflowPreviewIn),
        "create_evaluation": ("definition", EvaluationDefinition),
        "cancel_evaluation": ("request", WorkCancelIn),
        "review_evaluation_variant": ("request", EvaluationReviewIn),
        "run_scheduler": ("request", SchedulerRunIn),
    }
    actual = {
        route.operation_id: (
            parameter,
            get_type_hints(route.endpoint)[parameter],
        )
        for route in create_app(_composition()).routes
        if isinstance(route, APIRoute) and route.operation_id in expected
        for parameter, _model in (expected[route.operation_id],)
    }

    assert actual == expected


def test_workflow_request_accepts_the_complete_exact_input_set() -> None:
    request = WorkflowPreviewIn(
        recipe_id="fixture.recipe/v1",
        collection_ids=tuple(range(1, 1002)),
    )

    assert len(request.collection_ids) == 1001


def test_scheduler_work_failures_are_operator_visible(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _log_scheduler_failures(
        "controller",
        {
            "work": {
                "failures": [
                    {
                        "work_id": "work-1",
                        "error": "Conflict: processing outcomes differ",
                    }
                ]
            }
        },
    )

    assert (
        "stove0 controller scheduler could not advance work work-1: "
        "Conflict: processing outcomes differ"
    ) in caplog.text


def test_scheduler_event_failures_are_operator_visible(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _log_scheduler_failures(
        "controller",
        {
            "events": {
                "failures": [
                    {
                        "event_id": "event-1",
                        "error": "ValueError: invalid collection identity",
                    }
                ]
            },
            "work": {"failures": []},
        },
    )

    assert (
        "stove0 controller scheduler could not reconcile lifecycle event event-1: "
        "ValueError: invalid collection identity"
    ) in caplog.text


def test_stove0_openapi_uses_conventional_errors_health_and_paging() -> None:
    schema = create_app(_composition()).openapi()
    assert schema["components"]["schemas"]["HealthResponse"] == {
        "additionalProperties": False,
        "properties": {
            "service": {"minLength": 1, "title": "Service", "type": "string"},
            "status": {"const": "ok", "title": "Status", "type": "string"},
        },
        "required": ["service", "status"],
        "title": "HealthResponse",
        "type": "object",
    }
    for path in ("/v1/work", "/v1/evaluations"):
        operation = schema["paths"][path]["get"]
        assert {item["name"] for item in operation["parameters"]} >= {
            "page",
            "per_page",
            "all",
            "sort",
            "order",
        }
        assert operation["responses"]["200"]["content"]["application/json"]["schema"][
            "$ref"
        ].startswith("#/components/schemas/")
    selection = schema["paths"]["/v1/artifact-selections/{selection_sha256}"]["get"]
    assert {item["name"] for item in selection["parameters"]} >= {
        "selection_sha256",
        "page",
        "per_page",
        "all",
    }
    assert selection["responses"]["200"]["content"]["application/json"]["schema"][
        "$ref"
    ].startswith("#/components/schemas/")
    documented_error_sets: set[frozenset[str]] = set()
    for path, path_item in schema["paths"].items():
        if not path.startswith("/v1"):
            continue
        for method, operation in path_item.items():
            if method not in {"delete", "get", "patch", "post", "put"}:
                continue
            errors = frozenset(status for status in operation["responses"] if int(status) >= 400)
            documented_error_sets.add(errors)
            assert {"400", "401", "403", "500"} <= errors
            assert errors <= {"400", "401", "403", "404", "409", "500", "503"}
            assert "422" not in operation["responses"]
    assert len(documented_error_sets) > 1
    assert "404" in schema["paths"]["/v1/work/{work_id}"]["get"]["responses"]
    assert "409" in schema["paths"]["/v1/work"]["post"]["responses"]


def test_stove0_runtime_errors_use_the_shared_envelope() -> None:
    app = create_app(_composition())
    with TestClient(app) as client:
        assert client.get("/health/live").json() == {"service": "stove0", "status": "ok"}
        assert client.get("/health/ready").json() == {"service": "stove0", "status": "ok"}
        missing = client.get("/v1/events")
        assert missing.status_code == 401
        assert missing.json()["error"]["code"] == "unauthorized"
        invalid = client.get("/v1/events", headers={"Authorization": "Bearer invalid"})
        assert invalid.status_code == 401
        assert invalid.headers["www-authenticate"] == "Bearer"
        unknown = client.get(
            "/v1/work/" + "a" * 64,
            headers={"Authorization": "Bearer stove0-test-token"},
        )
        assert unknown.status_code == 404
        assert unknown.json() == {"error": {"code": "not_found", "message": "a" * 64}}
        malformed = client.post(
            "/v1/work",
            headers={"Authorization": "Bearer stove0-test-token"},
            json={"recipe_id": "fixture", "collection_ids": []},
        )
        assert malformed.status_code == 400
        assert malformed.json()["error"]["code"] == "bad_request"


def test_stove0_client_preserves_the_public_wire_error() -> None:
    app = create_app(_composition())
    with TestClient(app) as transport:
        client = Stove0ApiClient(
            "http://testserver",
            "invalid",
            allow_insecure_http=True,
        )
        client._client = cast(Any, TimeoutNeutralTestClient(transport))  # type: ignore[assignment]
        with pytest.raises(Stove0ApiError) as caught:
            client.list_events()

    error = caught.value
    assert error.message == "valid stove0 bearer credentials are required"
    assert error.code == "unauthorized"
    assert error.observed_status == 401
    assert error.details == {}


def test_stove0_client_transport_configuration_is_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STOVE0_BASE_URL", "https://stove0.example.test/")
    monkeypatch.setenv("STOVE0_TOKEN", "client-token")
    monkeypatch.setenv("STOVE0_HTTP2", "false")
    monkeypatch.setenv("STOVE0_HTTP_TIMEOUT_SECONDS", "17")

    client = Stove0ApiClient()
    try:
        assert client.base_url == "https://stove0.example.test"
        assert client.token == "client-token"
        assert client.http2 is False
        assert client.timeout_seconds == 17
    finally:
        client.close()


def test_installed_conformance_catalog_is_exact_through_api_client_and_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = Path(__file__).parents[3] / "qualification/fixtures/stove0/recipes.yaml"
    catalog = RecipeCatalog.load(path)
    application = create_app(replace(_lifecycle_composition(), recipes=catalog))

    with TestClient(application) as transport:
        client = Stove0ApiClient(
            "http://testserver",
            "stove0-test-token",
            allow_insecure_http=True,
        )
        client._client = cast(Any, TimeoutNeutralTestClient(transport))  # type: ignore[assignment]
        page = client.list_recipes()
        recipe = client.get_recipe("stove0.conformance-media/v1")
        preview = client.preview_workflow("stove0.conformance-media/v1", [1])

        def cli_client(*_args: object, **_kwargs: object) -> Stove0ApiClient:
            value = Stove0ApiClient(
                "http://testserver",
                "stove0-test-token",
                allow_insecure_http=True,
            )
            value._client = cast(  # type: ignore[assignment]
                Any,
                TimeoutNeutralTestClient(transport),
            )
            return value

        monkeypatch.setattr(stove0_cli, "Stove0ApiClient", cli_client)
        runner = CliRunner()
        human = runner.invoke(stove0_cli.app, ["preview", "stove0.conformance-media/v1", "1"])
        machine = runner.invoke(
            stove0_cli.app,
            ["--json", "preview", "stove0.conformance-media/v1", "1"],
        )

    assert page.catalog_sha256 == catalog.sha256
    identity = catalog.recipe("stove0.conformance-media/v1").identity_document()
    assert recipe.sha256 == identity["sha256"]
    assert preview.state == "ready"
    assert human.exit_code == 0, (human.output, human.exception)
    assert "ready" in human.stdout
    assert machine.exit_code == 0, (machine.output, machine.exception)
    assert json.loads(machine.stdout)["state"] == "ready"

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from lifecycle_events import EventPage
from riverhog_api_client import ApiClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from stove0_api.app import Stove0Composition, _log_scheduler_failures, create_app
from stove0_api_client import Stove0ApiClient
from stove0_core import (
    EvaluationService,
    RecipeCatalog,
    SqlAlchemyStateStore,
    Stove0Coordinator,
    Stove0RuntimeConfig,
    Stove0Scheduler,
    Stove0WorkService,
    WorkflowPreviewService,
)
from stove0_protocol import (
    CollectionRootRef,
    EvaluationDefinition,
    EvaluationDefinitionPayload,
    EvaluationMatrix,
    EvaluationMatrixPayload,
    EvaluationVariant,
    RecipeRef,
)

from tests.operation_observer import OperationObserver, TimeoutNeutralTestClient


class CatalogApi:
    def close(self) -> None:
        pass


class _Document:
    def __init__(self, **payload: object) -> None:
        self.payload = payload

    def model_dump(self, **_kwargs: object) -> dict[str, object]:
        return dict(self.payload)

    def __getattr__(self, name: str) -> object:
        try:
            return self.payload[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class _LifecycleCatalogApi(CatalogApi):
    def get_collection(self, collection_id: int) -> dict[str, object]:
        return {
            "id": collection_id,
            "manifest_sha256": "1" * 64,
            "content_etag": "2" * 64,
        }


class _LifecycleState:
    def __init__(self) -> None:
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.engine = engine

    def list_events(self, **_kwargs: object) -> EventPage:
        return EventPage(events=[], next_cursor="0", has_more=False)

    def list_work(self, **_kwargs: object) -> dict[str, object]:
        return {
            "page": 1,
            "per_page": 25,
            "total": 1,
            "pages": 1,
            "sort": "updated_at",
            "order": "desc",
            "filters": {},
            "work": [{"work_id": "work-1", "phase": "eligible", "revision": 1}],
        }

    def load(self, work_id: str) -> _Document | None:
        if work_id == "work-1":
            return _Document(work_id=work_id, phase="eligible", revision=1)
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
            "evaluations": [{"evaluation_id": "evaluation-1", "phase": "running", "revision": 1}],
        }

    def load_evaluation(self, evaluation_id: str) -> _Document:
        return _Document(evaluation_id=evaluation_id, phase="running", revision=1)

    def load_selection(self, selection_sha256: str) -> _Document:
        return _Document(
            selection_sha256=selection_sha256,
            total_bytes=12,
            artifacts=(
                _Document(
                    id="source",
                    role="fixture.source/v1",
                    path="source/input.bin",
                    bytes=12,
                ),
            ),
        )

    def load_cursor(self, _stream: str) -> None:
        return None


class _LifecycleRecipes:
    recipes = (_Document(id="fixture.recipe/v1", revision=1),)

    def recipe(self, recipe_id: str, revision: int | None) -> _Document:
        return _Document(id=recipe_id, revision=revision or 1)


class _LifecyclePlanner:
    def create_work(self, *_args: object, **_kwargs: object) -> object:
        return _Document(work_id="generated-work")


class _LifecycleCoordinator:
    planning = _LifecyclePlanner()

    def create_or_resume(self, _identity: object, **_kwargs: object) -> _Document:
        return _Document(work_id="work-1", phase="eligible", revision=1)

    def step(self, work_id: str) -> _Document:
        return _Document(work_id=work_id, phase="claimed", revision=2)

    def retry(self, work_id: str) -> _Document:
        return _Document(work_id=work_id, phase="eligible", revision=3)

    def cancel(self, work_id: str, *, reason: str | None) -> _Document:
        return _Document(work_id=work_id, phase="canceled", revision=4, reason=reason)

    def inspect_coordination(self, _work_id: str) -> _Document:
        return _Document(
            branch_set_sha256="b" * 64,
            unsettled_branch_ids=(),
            join_state="succeeded",
            branch_set_succeeded=True,
        )


class _LifecyclePreview:
    def preview(self, _identity: object) -> _Document:
        return _Document(
            format="stove0-workflow-preview/v1",
            state="ready",
            preview_sha256="a" * 64,
        )


class _LifecycleEvaluations:
    def _document(self, evaluation_id: str = "evaluation-1") -> _Document:
        return _Document(evaluation_id=evaluation_id, phase="running", revision=1)

    def create_or_resume(self, definition: EvaluationDefinition) -> _Document:
        return self._document(definition.evaluation_id)

    def refresh(self, evaluation_id: str) -> _Document:
        return self._document(evaluation_id)

    def step(self, evaluation_id: str, **_kwargs: object) -> _Document:
        return self._document(evaluation_id)

    def cancel(self, evaluation_id: str, **_kwargs: object) -> _Document:
        return _Document(evaluation_id=evaluation_id, phase="canceled", revision=2)

    def retry_failed(self, evaluation_id: str, _variant_id: str, **_kwargs: object) -> _Document:
        return self._document(evaluation_id)

    def review(self, evaluation_id: str, _review: object) -> _Document:
        return self._document(evaluation_id)


class _LifecycleScheduler:
    def run_once(self, **_kwargs: object) -> dict[str, object]:
        return {"role": "combined", "events": 0, "work": 0}


def _lifecycle_composition() -> Stove0Composition:
    state = _LifecycleState()
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
        recipes=cast(RecipeCatalog, _LifecycleRecipes()),
        work=cast(Stove0WorkService, object()),
        coordinator=cast(Stove0Coordinator, _LifecycleCoordinator()),
        preview=cast(WorkflowPreviewService, _LifecyclePreview()),
        evaluations=cast(EvaluationService, _LifecycleEvaluations()),
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
                    manifest_sha256="1" * 64,
                    content_etag="2" * 64,
                ),
            ),
            matrix=matrix,
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

        assert client.health_live()["status"] == "ok"
        assert client.health_ready()["status"] == "ok"
        assert client.list_events().next_cursor == "0"
        assert client.list_recipes()["recipes"]
        assert client.get_recipe("fixture.recipe/v1")["revision"] == 1
        assert client.list_work(all_items=True)["total"] == 1
        assert (
            client.create_work(
                "fixture.recipe/v1",
                [1],
                preview_sha256="a" * 64,
            )["work_id"]
            == "work-1"
        )
        assert client.get_work("work-1")["work_id"] == "work-1"
        assert client.inspect_work_coordination("work-1")["branch_set_succeeded"] is True
        assert client.get_artifact_selection("c" * 64)["total"] == 1
        assert client.step_work("work-1")["phase"] == "claimed"
        assert client.retry_work("work-1")["phase"] == "eligible"
        assert client.cancel_work("work-1", reason="qualification")["phase"] == "canceled"
        assert client.preview_workflow("fixture.recipe/v1", [1])["state"] == "ready"
        assert client.list_evaluations(all_items=True)["total"] == 1
        assert client.create_evaluation(definition.model_dump(mode="json"))["evaluation_id"] == (
            evaluation_id
        )
        assert client.get_evaluation(evaluation_id)["evaluation_id"] == evaluation_id
        assert client.step_evaluation(evaluation_id)["evaluation_id"] == evaluation_id
        assert client.cancel_evaluation(evaluation_id, reason="qualification")["phase"] == (
            "canceled"
        )
        assert (
            client.retry_evaluation_variant(evaluation_id, "variant-a")["evaluation_id"]
            == evaluation_id
        )
        assert (
            client.review_evaluation_variant(
                evaluation_id,
                "variant-a",
                rating=5,
            )["evaluation_id"]
            == evaluation_id
        )
        assert client.scheduler_status()["roles"] == ["controller", "worker", "combined"]
        assert client.run_scheduler(role="combined")["role"] == "combined"
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
    for path, path_item in schema["paths"].items():
        if not path.startswith("/v1"):
            continue
        for method, operation in path_item.items():
            if method not in {"delete", "get", "patch", "post", "put"}:
                continue
            assert {status for status in operation["responses"] if int(status) >= 400} == {
                "400",
                "401",
                "403",
                "404",
                "409",
                "500",
                "503",
            }
            assert "422" not in operation["responses"]


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

"""Authenticated HTTP surface for the single stove0 workflow authority."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import logging
import secrets
import signal
import sys
import threading
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, cast

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from http_api_contracts import (
    BrowsePageToken,
    BrowseQuery,
    apply_openapi_error_contract,
    cursor_feed_operation,
    error_code_for_status,
    error_payload,
    exact_authority_page_operation,
    mutable_browse_operation,
    operation_interface,
)
from http_api_contracts.browse import BrowseTokenCodec, BrowseTokenError
from pydantic import ValidationError
from riverhog_api_client import ApiClient
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException
from state_schema import StateSchemaError
from stove0_core import (
    ConcurrentEvaluationUpdate,
    ConcurrentWorkUpdate,
    EvaluationReview,
    EvaluationService,
    HttpObserverPort,
    HttpTargetPort,
    RecipeCatalog,
    RecipePlanner,
    SchedulerRole,
    SqlAlchemyStateStore,
    Stove0Coordinator,
    Stove0RiverhogClient,
    Stove0RuntimeConfig,
    Stove0Scheduler,
    Stove0StateError,
    Stove0WorkService,
    TargetCallbackAuthority,
    WorkflowPreviewService,
    database_url_from_environment,
    scheduler_role,
    stove0_state_schema,
)
from stove0_observer_client import ContentObserverClient, load_semantic_validator_registry
from stove0_operator_contracts import (
    EvaluationPage,
    EvaluationPhase,
    EvaluationSort,
    EvaluationView,
    RecipeCatalogView,
    RecipeView,
    SchedulerRun,
    SchedulerStatus,
    SortOrder,
    Stove0EventPage,
    WorkPage,
    WorkPhase,
    WorkSort,
    WorkView,
)
from stove0_protocol import (
    ARTIFACT_SELECTION_PAGE_MAX,
    ArtifactSelectionPage,
    BranchSetEvaluation,
    EvaluationDefinition,
    WorkflowPreview,
    WorkIdentity,
)
from stove0_target_client import TargetClient
from stove0_target_protocol import (
    InputDispositionDeclaration,
    OutputArtifact,
    OutputSourceEdge,
    TargetCallbackAcknowledgement,
    TargetInputPage,
    TargetProductionSealResponse,
)
from time_formats import utc_timestamp_now

from stove0_api.error_contracts import STOVE0_OPERATION_ERROR_CODES
from stove0_api.schemas import (
    ErrorResponse,
    EvaluationReviewIn,
    HealthResponse,
    SchedulerRunIn,
    WorkCreateIn,
    WorkflowPreviewIn,
)

type BrowsePageTokenQuery = Annotated[BrowsePageToken | None, Query()]
type BrowseQueryParameter = Annotated[BrowseQuery | None, Query()]

LOGGER = logging.getLogger(__name__)


def _error_response(
    status_code: int,
    *,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=error_payload(code=code, message=message),
        headers=headers,
    )


@dataclass(frozen=True, slots=True)
class Stove0Composition:
    config: Stove0RuntimeConfig
    riverhog_api: ApiClient
    state: SqlAlchemyStateStore
    recipes: RecipeCatalog
    work: Stove0WorkService
    coordinator: Stove0Coordinator
    preview: WorkflowPreviewService
    evaluations: EvaluationService
    scheduler: Stove0Scheduler
    target_callbacks: TargetCallbackAuthority | None = None
    browse_tokens: BrowseTokenCodec | None = None

    @classmethod
    def build(cls, config: Stove0RuntimeConfig) -> Stove0Composition:
        riverhog_api = ApiClient(
            base_url=config.riverhog_base_url,
            token=config.riverhog_token,
            allow_insecure_http=config.riverhog_allow_insecure_http,
        )
        stove0_state_schema(config.database_url).validate()
        state = SqlAlchemyStateStore(config.database_url, initialize=False)
        observers = HttpObserverPort(
            {
                key: ContentObserverClient(
                    value.base_url,
                    token=value.token,
                    allow_insecure_http=value.allow_insecure_http,
                    semantic_validators=load_semantic_validator_registry(
                        value.semantic_validator_providers
                    ),
                )
                for key, value in config.observers.items()
            }
        )
        targets = HttpTargetPort(
            {
                key: TargetClient(
                    value.base_url,
                    token=value.token,
                    allow_insecure_http=value.allow_insecure_http,
                )
                for key, value in config.targets.items()
            }
        )
        recipes = RecipeCatalog.load(config.recipes_path)
        planner = RecipePlanner(
            catalog=recipes,
            riverhog=riverhog_api,
            observers=observers,
            targets=targets,
        )
        work = Stove0WorkService(state)
        authority = Stove0RiverhogClient(
            riverhog_api,
            claim_lease_seconds=config.claim_lease_seconds,
            capability_ttl_seconds=config.capability_ttl_seconds,
            workspace_assurance=config.workspace_assurance,
            state=state,
            authority_batch_size=config.target_authority_batch_size,
        )
        target_callbacks = TargetCallbackAuthority(
            state,
            signing_key=config.target_callback_signing_key,
            base_url=config.target_callback_base_url,
            allow_insecure_http=config.target_callback_allow_insecure_http,
            ttl_seconds=config.capability_ttl_seconds,
            operations=planner,
            projector=authority,
            seal_batch_size=config.target_authority_batch_size,
        )
        coordinator = Stove0Coordinator(
            work,
            riverhog=authority,
            planning=planner,
            observers=observers,
            targets=targets,
            target_callbacks=target_callbacks,
        )
        return cls(
            config=config,
            riverhog_api=riverhog_api,
            state=state,
            recipes=recipes,
            work=work,
            coordinator=coordinator,
            preview=WorkflowPreviewService(
                riverhog=authority,
                planning=planner,
                observers=observers,
                targets=targets,
            ),
            evaluations=EvaluationService(state.evaluation_store(), work=work),
            scheduler=Stove0Scheduler(
                coordinator=coordinator,
                state=state,
                production_seals=target_callbacks,
                operational_state_retention_seconds=(config.operational_state_retention_seconds),
            ),
            target_callbacks=target_callbacks,
            browse_tokens=BrowseTokenCodec(
                config.browse_token_signing_key,
                lifetime_seconds=config.browse_token_lifetime_seconds,
            ),
        )


def _bearer(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.casefold() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Bearer authentication is required")
    return token.strip()


def _target_callback(composition: Stove0Composition) -> TargetCallbackAuthority:
    callbacks = composition.target_callbacks
    if callbacks is None:
        raise HTTPException(status_code=503, detail="target callbacks are unavailable")
    return callbacks


def create_app(
    composition: Stove0Composition,
) -> FastAPI:
    api_token = composition.config.api_token
    if api_token is None:
        raise ValueError("STOVE0_API_TOKEN is required by the stove0 operator API")

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            composition.riverhog_api.close()
            composition.state.engine.dispose()

    app = FastAPI(
        title="stove0",
        version="1",
        lifespan=lifespan,
        openapi_url="/v1/openapi.json",
    )

    def authorize(token: str = Depends(_bearer)) -> None:
        if not secrets.compare_digest(token, api_token):
            raise HTTPException(
                status_code=401,
                detail="valid stove0 bearer credentials are required",
                headers={"WWW-Authenticate": "Bearer"},
            )

    browse_tokens = composition.browse_tokens or BrowseTokenCodec(
        composition.config.browse_token_signing_key,
        lifetime_seconds=composition.config.browse_token_lifetime_seconds,
    )
    # The bearer credential authenticates this single operator principal.  A
    # credential rotation does not create a new principal, and authorization is
    # still reevaluated above for every bounded page request.
    operator_principal = {"principal": "stove0-operator/v1"}

    def browse_position(
        *,
        operation: str,
        page_token: str | None,
        selectors: dict[str, object],
    ) -> tuple[str | int | bool | bytes | None, ...] | None:
        try:
            return browse_tokens.verify(
                page_token,
                operation=operation,
                principal=operator_principal,
                selectors=selectors,
            )
        except BrowseTokenError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def browse_page(
        payload: dict[str, object],
        *,
        operation: str,
        selectors: dict[str, object],
    ) -> dict[str, object]:
        position = payload.pop("_next_position", None)
        if position is not None and (
            isinstance(position, (str, bytes)) or not isinstance(position, Sequence)
        ):
            raise RuntimeError("stove0 browse service returned an invalid continuation")
        payload["next_page_token"] = (
            None
            if position is None
            else browse_tokens.issue(
                operation=operation,
                principal=operator_principal,
                selectors=selectors,
                position=position,
            )
        )
        return payload

    @app.get(
        "/v1/target-executions/{job_id}/inputs",
        response_model=TargetInputPage,
        operation_id="get_target_execution_inputs",
        tags=["target-executions"],
        openapi_extra={
            **operation_interface("client-only-primitive"),
            **exact_authority_page_operation(
                authority="target-input-authority",
                authority_parameter=None,
                cursor_parameter="continuation",
                fixed_limit=256,
            ),
        },
    )
    def get_target_execution_inputs(
        job_id: str,
        continuation: str | None = None,
        token: str = Depends(_bearer),
    ) -> TargetInputPage:
        callbacks = composition.target_callbacks
        if callbacks is None:
            raise HTTPException(status_code=503, detail="target callbacks are unavailable")
        try:
            return callbacks.input_page(
                token,
                job_id=job_id,
                continuation=continuation,
                limit=256,
            )
        except PermissionError as exc:
            raise HTTPException(
                status_code=401,
                detail=str(exc),
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

    @app.put(
        "/v1/target-executions/{job_id}/outputs/{artifact_id}",
        response_model=TargetCallbackAcknowledgement,
        operation_id="declare_target_execution_output",
        tags=["target-executions"],
        openapi_extra=operation_interface("client-only-primitive"),
    )
    def declare_target_execution_output(
        job_id: str,
        artifact_id: str,
        output: OutputArtifact,
        token: str = Depends(_bearer),
    ) -> TargetCallbackAcknowledgement:
        if artifact_id != output.id:
            raise HTTPException(status_code=409, detail="output path identity differs")
        _target_callback(composition).declare_output(token, job_id=job_id, output=output)
        return TargetCallbackAcknowledgement()

    @app.put(
        "/v1/target-executions/{job_id}/dispositions/{input_id}",
        response_model=TargetCallbackAcknowledgement,
        operation_id="declare_target_execution_disposition",
        tags=["target-executions"],
        openapi_extra=operation_interface("client-only-primitive"),
    )
    def declare_target_execution_disposition(
        job_id: str,
        input_id: str,
        disposition: InputDispositionDeclaration,
        token: str = Depends(_bearer),
    ) -> TargetCallbackAcknowledgement:
        if input_id != disposition.input_id:
            raise HTTPException(status_code=409, detail="disposition path identity differs")
        _target_callback(composition).declare_disposition(
            token,
            job_id=job_id,
            disposition=disposition,
        )
        return TargetCallbackAcknowledgement()

    @app.put(
        "/v1/target-executions/{job_id}/source-edges/{output_id}/{input_id}",
        response_model=TargetCallbackAcknowledgement,
        operation_id="declare_target_execution_source_edge",
        tags=["target-executions"],
        openapi_extra=operation_interface("client-only-primitive"),
    )
    def declare_target_execution_source_edge(
        job_id: str,
        output_id: str,
        input_id: str,
        edge: OutputSourceEdge,
        token: str = Depends(_bearer),
    ) -> TargetCallbackAcknowledgement:
        if output_id != edge.output_id or input_id != edge.input_id:
            raise HTTPException(status_code=409, detail="source-edge path identity differs")
        _target_callback(composition).declare_source_edge(token, job_id=job_id, edge=edge)
        return TargetCallbackAcknowledgement()

    @app.post(
        "/v1/target-executions/{job_id}/production/seal",
        response_model=TargetProductionSealResponse,
        operation_id="seal_target_execution_production",
        tags=["target-executions"],
        openapi_extra=operation_interface("client-only-primitive"),
    )
    def seal_target_execution_production(
        job_id: str,
        token: str = Depends(_bearer),
    ) -> TargetProductionSealResponse:
        return _target_callback(composition).seal_production(token, job_id=job_id)

    @app.exception_handler(KeyError)
    async def missing(_request: Request, exc: KeyError) -> JSONResponse:
        return _error_response(404, code="not_found", message=str(exc.args[0]))

    @app.exception_handler(PermissionError)
    async def target_callback_unauthorized(
        _request: Request,
        exc: PermissionError,
    ) -> JSONResponse:
        return _error_response(
            401,
            code="unauthorized",
            message=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.exception_handler(RequestValidationError)
    @app.exception_handler(ValidationError)
    @app.exception_handler(ValueError)
    async def invalid(_request: Request, exc: Exception) -> JSONResponse:
        return _error_response(400, code="bad_request", message=str(exc))

    @app.exception_handler(ConcurrentWorkUpdate)
    @app.exception_handler(ConcurrentEvaluationUpdate)
    @app.exception_handler(Stove0StateError)
    async def conflict(_request: Request, exc: Exception) -> JSONResponse:
        return _error_response(409, code="conflict", message=str(exc))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _error_response(
            exc.status_code,
            code=error_code_for_status(exc.status_code),
            message=str(exc.detail),
            headers=dict(exc.headers or {}),
        )

    @app.exception_handler(Exception)
    async def unexpected(_request: Request, exc: Exception) -> JSONResponse:
        LOGGER.exception("unhandled stove0 API error", exc_info=exc)
        return _error_response(500, code="internal_error", message="internal server error")

    @app.get(
        "/health/live",
        response_model=HealthResponse,
        operation_id="health_live",
        tags=["health"],
    )
    def health_live() -> HealthResponse:
        return HealthResponse(service="stove0", status="ok")

    @app.get(
        "/health/ready",
        response_model=HealthResponse,
        responses={503: {"model": ErrorResponse}},
        operation_id="health_ready",
        tags=["health"],
    )
    def health_ready() -> HealthResponse:
        try:
            with composition.state.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="stove0 state database is not ready",
            ) from exc
        return HealthResponse(service="stove0", status="ok")

    @app.get(
        "/v1/events",
        response_model=Stove0EventPage,
        dependencies=[Depends(authorize)],
        operation_id="list_events",
        tags=["events"],
        openapi_extra=cursor_feed_operation(cursor_parameter="after", limit_parameter="limit"),
    )
    def list_events(
        after: str | None = None,
        limit: int = Query(default=100, ge=1, le=100),
    ) -> Stove0EventPage:
        return composition.state.list_events(after=after, limit=limit)

    @app.get(
        "/v1/recipes",
        response_model=RecipeCatalogView,
        dependencies=[Depends(authorize)],
        operation_id="list_recipes",
        tags=["recipes"],
    )
    def list_recipes() -> RecipeCatalogView:
        return RecipeCatalogView(
            catalog_sha256=composition.recipes.sha256,
            recipes=tuple(
                RecipeView.from_definition(recipe) for recipe in composition.recipes.recipes
            ),
        )

    @app.get(
        "/v1/recipes/{recipe_id:path}",
        response_model=RecipeView,
        dependencies=[Depends(authorize)],
        operation_id="get_recipe",
        tags=["recipes"],
    )
    def get_recipe(recipe_id: str, revision: int | None = None) -> RecipeView:
        recipe = composition.recipes.recipe(recipe_id, revision)
        return RecipeView.from_definition(recipe)

    @app.get(
        "/v1/work",
        response_model=WorkPage,
        dependencies=[Depends(authorize)],
        operation_id="list_work",
        tags=["work"],
        openapi_extra=mutable_browse_operation(),
    )
    def list_work(
        page_size: int = Query(default=25, ge=1, le=100),
        page_token: BrowsePageTokenQuery = None,
        phase: WorkPhase | None = None,
        q: BrowseQueryParameter = None,
        sort: WorkSort = "updated_at",
        order: SortOrder = "desc",
    ) -> WorkPage:
        normalized_query = q.casefold() if q is not None else None
        selectors: dict[str, object] = {
            "phase": phase,
            "q": normalized_query,
            "sort": sort,
            "order": order,
        }
        position = browse_position(
            operation="list_work", page_token=page_token, selectors=selectors
        )
        return WorkPage.from_page(
            browse_page(
                composition.state.list_work(
                    page_size=page_size,
                    position=position,
                    phase=phase,
                    query=normalized_query,
                    sort=sort,
                    order=order,
                ),
                operation="list_work",
                selectors=selectors,
            )
        )

    @app.post(
        "/v1/work",
        status_code=201,
        response_model=WorkView,
        dependencies=[Depends(authorize)],
        operation_id="create_work",
        tags=["work"],
    )
    def create_work(request: WorkCreateIn) -> WorkView:
        identity = _work_identity(composition, request)
        existing = composition.state.load(identity.work_id)
        if existing is not None:
            acceptance = existing.preview_acceptance
            if acceptance is None or acceptance.preview_sha256 != request.preview_sha256:
                raise HTTPException(
                    status_code=409,
                    detail="existing work was not initiated from the accepted preview",
                )
            return WorkView.from_record(existing)
        preview = composition.preview.preview(identity)
        if preview.state != "ready" or preview.preview_sha256 != request.preview_sha256:
            raise HTTPException(
                status_code=409,
                detail="current workflow preview differs from the accepted preview",
            )
        return WorkView.from_record(
            composition.coordinator.create_or_resume(identity, preview=preview)
        )

    @app.get(
        "/v1/work/{work_id}",
        response_model=WorkView,
        dependencies=[Depends(authorize)],
        operation_id="get_work",
        tags=["work"],
    )
    def get_work(work_id: str) -> WorkView:
        record = composition.state.load(work_id)
        if record is None:
            raise KeyError(work_id)
        return WorkView.from_record(record)

    @app.get(
        "/v1/work/{work_id}/coordination",
        response_model=BranchSetEvaluation,
        dependencies=[Depends(authorize)],
        operation_id="inspect_work_coordination",
        tags=["work"],
    )
    def inspect_work_coordination(work_id: str) -> BranchSetEvaluation:
        return composition.coordinator.inspect_coordination(work_id)

    @app.get(
        "/v1/artifact-selections/{selection_sha256}",
        response_model=ArtifactSelectionPage,
        dependencies=[Depends(authorize)],
        operation_id="get_artifact_selection",
        tags=["artifact-selections"],
        openapi_extra=exact_authority_page_operation(
            authority="artifact-selection",
            authority_parameter="selection_sha256",
            cursor_parameter="continuation",
            fixed_limit=256,
        ),
    )
    def get_artifact_selection(
        selection_sha256: str,
        continuation: str | None = None,
    ) -> ArtifactSelectionPage:
        selection = composition.state.load_selection_ref(selection_sha256)
        if selection is None:
            raise KeyError(selection_sha256)
        selected, next_continuation, complete = composition.state.selection_artifact_page(
            selection_sha256,
            continuation=continuation,
            limit=ARTIFACT_SELECTION_PAGE_MAX,
        )
        return ArtifactSelectionPage(
            authority=selection,
            continuation=continuation,
            next_continuation=next_continuation,
            complete=complete,
            artifacts=selected,
        )

    @app.post(
        "/v1/work/{work_id}/step",
        response_model=WorkView,
        dependencies=[Depends(authorize)],
        operation_id="step_work",
        tags=["work"],
    )
    def step_work(work_id: str) -> WorkView:
        return WorkView.from_record(composition.coordinator.step(work_id))

    @app.post(
        "/v1/work/{work_id}/retry",
        response_model=WorkView,
        dependencies=[Depends(authorize)],
        operation_id="retry_work",
        tags=["work"],
    )
    def retry_work(work_id: str) -> WorkView:
        return WorkView.from_record(composition.coordinator.retry(work_id))

    @app.post(
        "/v1/work/{work_id}/cancel",
        response_model=WorkView,
        dependencies=[Depends(authorize)],
        operation_id="cancel_work",
        tags=["work"],
    )
    def cancel_work(work_id: str) -> WorkView:
        return WorkView.from_record(composition.coordinator.cancel(work_id))

    @app.post(
        "/v1/workflow-previews",
        response_model=WorkflowPreview,
        dependencies=[Depends(authorize)],
        operation_id="preview_workflow",
        tags=["previews"],
    )
    def preview_workflow(request: WorkflowPreviewIn) -> WorkflowPreview:
        return composition.preview.preview(_work_identity(composition, request))

    @app.get(
        "/v1/evaluations",
        response_model=EvaluationPage,
        dependencies=[Depends(authorize)],
        operation_id="list_evaluations",
        tags=["evaluations"],
        openapi_extra=mutable_browse_operation(),
    )
    def list_evaluations(
        page_size: int = Query(default=25, ge=1, le=100),
        page_token: BrowsePageTokenQuery = None,
        phase: EvaluationPhase | None = None,
        q: BrowseQueryParameter = None,
        sort: EvaluationSort = "updated_at",
        order: SortOrder = "desc",
    ) -> EvaluationPage:
        normalized_query = q.casefold() if q is not None else None
        selectors: dict[str, object] = {
            "phase": phase,
            "q": normalized_query,
            "sort": sort,
            "order": order,
        }
        position = browse_position(
            operation="list_evaluations", page_token=page_token, selectors=selectors
        )
        return EvaluationPage.from_page(
            browse_page(
                composition.state.list_evaluations(
                    page_size=page_size,
                    position=position,
                    phase=phase,
                    query=normalized_query,
                    sort=sort,
                    order=order,
                ),
                operation="list_evaluations",
                selectors=selectors,
            )
        )

    @app.post(
        "/v1/evaluations",
        status_code=201,
        response_model=EvaluationView,
        dependencies=[Depends(authorize)],
        operation_id="create_evaluation",
        tags=["evaluations"],
    )
    def create_evaluation(definition: EvaluationDefinition) -> EvaluationView:
        return EvaluationView.from_record(composition.evaluations.create_or_resume(definition))

    @app.get(
        "/v1/evaluations/{evaluation_id}",
        response_model=EvaluationView,
        dependencies=[Depends(authorize)],
        operation_id="get_evaluation",
        tags=["evaluations"],
    )
    def get_evaluation(evaluation_id: str) -> EvaluationView:
        record = composition.state.load_evaluation(evaluation_id)
        if record is None:
            raise KeyError(evaluation_id)
        return EvaluationView.from_record(composition.evaluations.refresh(evaluation_id))

    @app.post(
        "/v1/evaluations/{evaluation_id}/step",
        response_model=EvaluationView,
        dependencies=[Depends(authorize)],
        operation_id="step_evaluation",
        tags=["evaluations"],
    )
    def step_evaluation(evaluation_id: str) -> EvaluationView:
        return EvaluationView.from_record(
            composition.evaluations.step(
                evaluation_id,
                controller=composition.coordinator,
            )
        )

    @app.post(
        "/v1/evaluations/{evaluation_id}/cancel",
        response_model=EvaluationView,
        dependencies=[Depends(authorize)],
        operation_id="cancel_evaluation",
        tags=["evaluations"],
    )
    def cancel_evaluation(evaluation_id: str) -> EvaluationView:
        return EvaluationView.from_record(
            composition.evaluations.cancel(
                evaluation_id,
                controller=composition.coordinator,
            )
        )

    @app.post(
        "/v1/evaluations/{evaluation_id}/variants/{variant_id}/retry",
        response_model=EvaluationView,
        dependencies=[Depends(authorize)],
        operation_id="retry_evaluation_variant",
        tags=["evaluations"],
    )
    def retry_evaluation_variant(evaluation_id: str, variant_id: str) -> EvaluationView:
        return EvaluationView.from_record(
            composition.evaluations.retry_failed(
                evaluation_id,
                variant_id,
                controller=composition.coordinator,
            )
        )

    @app.put(
        "/v1/evaluations/{evaluation_id}/variants/{variant_id}/review",
        response_model=EvaluationView,
        dependencies=[Depends(authorize)],
        operation_id="review_evaluation_variant",
        tags=["evaluations"],
    )
    def review_evaluation_variant(
        evaluation_id: str,
        variant_id: str,
        request: EvaluationReviewIn,
    ) -> EvaluationView:
        review = EvaluationReview(
            variant_id=variant_id,
            rating=request.rating,
            note=request.note,
            updated_by="operator",
            updated_at=utc_timestamp_now(),
        )
        return EvaluationView.from_record(composition.evaluations.review(evaluation_id, review))

    @app.get(
        "/v1/admin/scheduler",
        response_model=SchedulerStatus,
        dependencies=[Depends(authorize)],
        operation_id="scheduler_status",
        tags=["scheduler"],
    )
    def scheduler_status() -> SchedulerStatus:
        return SchedulerStatus(
            running=False,
            interval_seconds=composition.config.scheduler_interval_seconds,
            roles=("controller", "worker", "combined"),
        )

    @app.post(
        "/v1/admin/scheduler/run",
        response_model=SchedulerRun,
        dependencies=[Depends(authorize)],
        operation_id="run_scheduler",
        tags=["scheduler"],
    )
    def run_scheduler_once(request: SchedulerRunIn) -> SchedulerRun:
        return SchedulerRun.model_validate(
            composition.scheduler.run_once(
                role=request.role,
                work_limit=request.work_limit,
            )
        )

    app.openapi_schema = apply_openapi_error_contract(
        app.openapi(),
        operation_error_codes=STOVE0_OPERATION_ERROR_CODES,
    )
    return app


def _work_identity(
    composition: Stove0Composition,
    request: WorkflowPreviewIn,
) -> WorkIdentity:
    for root in request.inputs:
        current = composition.riverhog_api.get_collection(root.collection_id)
        if (
            str(current.get("archive_root_sha256") or "") != root.archive_root_sha256
            or str(current.get("content_identity") or "") != root.content_identity
        ):
            raise HTTPException(
                status_code=409,
                detail=f"collection receipt differs from Riverhog: {root.collection_id}",
            )
    planner = cast(RecipePlanner, composition.coordinator.planning)
    return planner.create_work(
        request.recipe_id,
        list(request.inputs),
        revision=request.recipe_revision,
        effective_intent=request.effective_intent,
    )


def _scheduler_loop(
    composition: Stove0Composition,
    role: SchedulerRole,
    stop: threading.Event,
) -> None:
    try:
        while not stop.is_set():
            try:
                result = composition.scheduler.run_once(role=role)
                _log_scheduler_failures(role, result)
            except Exception:
                # The durable cursor/work state makes the next tick a safe retry.
                LOGGER.exception("stove0 %s scheduler iteration failed", role)
            stop.wait(composition.config.scheduler_interval_seconds)
    finally:
        composition.riverhog_api.close()
        composition.state.engine.dispose()


def _log_scheduler_failures(role: SchedulerRole, result: dict[str, object]) -> None:
    """Make isolated work advancement failures operator-visible."""

    work = result.get("work")
    failures = work.get("failures") if isinstance(work, dict) else None
    if not isinstance(failures, list):
        return
    for failure in failures:
        if not isinstance(failure, dict):
            continue
        LOGGER.error(
            "stove0 %s scheduler could not advance work %s: %s",
            role,
            failure.get("work_id", "unknown"),
            failure.get("error", "unknown error"),
        )


def _install_stop_handlers(stop: threading.Event) -> None:
    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stove0-server")
    parser.add_argument(
        "--version",
        action="version",
        version=importlib.metadata.version("stove0-server"),
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    serve = subcommands.add_parser("serve", help="run the authenticated operator API")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8080)
    scheduler = subcommands.add_parser("scheduler", help="run one durable scheduler role")
    scheduler.add_argument(
        "--role",
        choices=("controller", "worker", "combined"),
        default="combined",
    )
    state = subcommands.add_parser("state", help="inspect or upgrade control-state schema")
    state_subcommands = state.add_subparsers(dest="state_command", required=True)
    for command_name, help_text in (
        ("status", "show the current and required control-state revisions"),
        ("upgrade", "explicitly upgrade control state to the current revision"),
        ("verify", "verify the current revision and exact control-state schema"),
    ):
        command_parser = state_subcommands.add_parser(command_name, help=help_text)
        command_parser.add_argument("--json", action="store_true", help="Emit JSON.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "state":
        schema = stove0_state_schema(database_url_from_environment())
        try:
            if args.state_command == "status":
                status = schema.status()
            elif args.state_command == "upgrade":
                status = schema.upgrade()
            else:
                status = schema.validate()
        except StateSchemaError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        payload = status.as_dict()
        if args.json:
            print(json.dumps(payload, sort_keys=True))
        else:
            print(
                f"stove0 control state: {payload['condition']} "
                f"({payload['current_revision'] or 'none'} -> {payload['head_revision']})"
            )
        return 0
    config = Stove0RuntimeConfig.from_environment(
        require_api_token=args.command == "serve",
    )
    composition = Stove0Composition.build(config)
    if args.command == "serve":
        uvicorn.run(
            create_app(composition),
            host=str(args.host),
            port=int(args.port),
        )
        return 0
    stop = threading.Event()
    _install_stop_handlers(stop)
    _scheduler_loop(composition, scheduler_role(str(args.role)), stop)
    return 0


__all__ = ["Stove0Composition", "create_app", "main"]

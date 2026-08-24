"""Official stove0 v1 HTTP client."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping, Sequence
from typing import Any, Literal, Self
from urllib.parse import quote

import httpx
from http_api_contracts import parse_error_payload, safe_http_base_url
from pydantic import BaseModel, ConfigDict, Field
from stove0_operator_contracts import (
    ArtifactSelectionPage,
    EvaluationPage,
    EvaluationPhase,
    EvaluationReviewIn,
    EvaluationView,
    RecipeCatalogView,
    RecipeView,
    SchedulerRole,
    SchedulerRun,
    SchedulerRunIn,
    SchedulerStatus,
    Stove0EventPage,
    WorkCancelIn,
    WorkCreateIn,
    WorkflowPreviewIn,
    WorkPage,
    WorkPhase,
    WorkView,
)
from stove0_protocol import BranchSetEvaluation, EvaluationDefinition, WorkflowPreview

type _WorkSort = Literal["updated_at", "phase", "work_id"]
type _EvaluationSort = Literal["updated_at", "phase", "evaluation_id"]
type _SortOrder = Literal["asc", "desc"]


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str = Field(min_length=1)
    status: Literal["ok"]


def _one_of(value: str, allowed: frozenset[str], label: str) -> str:
    if value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"{label} must be one of: {choices}")
    return value


class Stove0ApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "stove0_client_error",
        observed_status: int | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        if observed_status is not None and not 400 <= observed_status <= 599:
            raise ValueError("observed HTTP status must be a 4xx or 5xx response")
        self.message = message
        self.code = code
        self.observed_status = observed_status
        self.details = dict(details or {})


class Stove0ApiClient:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        allow_insecure_http: bool | None = None,
        timeout_seconds: float | None = None,
        http2: bool | None = None,
    ) -> None:
        allow = (
            _boolean_env("STOVE0_ALLOW_INSECURE_HTTP", False)
            if allow_insecure_http is None
            else allow_insecure_http
        )
        self.base_url = safe_http_base_url(
            base_url or os.getenv("STOVE0_BASE_URL") or "http://127.0.0.1:8080",
            setting="STOVE0_BASE_URL",
            allow_insecure_http=allow,
        )
        self.allow_insecure_http = allow
        self.token = token or os.getenv("STOVE0_TOKEN")
        self.http2 = _boolean_env("STOVE0_HTTP2", True) if http2 is None else http2
        self.timeout_seconds = (
            _positive_float_env("STOVE0_HTTP_TIMEOUT_SECONDS", 300.0)
            if timeout_seconds is None
            else _positive_float(timeout_seconds, "timeout_seconds")
        )
        self._client: httpx.Client | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def health_live(self) -> HealthResponse:
        return HealthResponse.model_validate(self._json("GET", "/health/live", authenticated=False))

    def health_ready(self) -> HealthResponse:
        return HealthResponse.model_validate(
            self._json("GET", "/health/ready", authenticated=False)
        )

    def list_events(self, *, after: str | None = None, limit: int = 100) -> Stove0EventPage:
        return Stove0EventPage.model_validate(
            self._json("GET", "/v1/events", params=_params(after=after, limit=limit))
        )

    def list_recipes(self) -> RecipeCatalogView:
        return RecipeCatalogView.model_validate(self._json("GET", "/v1/recipes"))

    def get_recipe(self, recipe_id: str, *, revision: int | None = None) -> RecipeView:
        return RecipeView.model_validate(
            self._json(
                "GET",
                f"/v1/recipes/{quote(recipe_id, safe='')}",
                params=_params(revision=revision),
            )
        )

    def list_work(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        phase: WorkPhase | None = None,
        query: str | None = None,
        sort: _WorkSort = "updated_at",
        order: _SortOrder = "desc",
        all_items: bool = False,
    ) -> WorkPage:
        return WorkPage.model_validate(
            self._json(
                "GET",
                "/v1/work",
                params=_params(
                    **{
                        "page": page,
                        "per_page": per_page,
                        "phase": phase,
                        "q": query,
                        "sort": _one_of(
                            sort,
                            frozenset({"updated_at", "phase", "work_id"}),
                            "work sort",
                        ),
                        "order": _one_of(
                            order,
                            frozenset({"asc", "desc"}),
                            "sort order",
                        ),
                        "all": all_items,
                    }
                ),
            )
        )

    def create_work(
        self,
        recipe_id: str,
        collection_ids: Sequence[int],
        *,
        preview_sha256: str,
        recipe_revision: int | None = None,
        effective_intent: Mapping[str, Any] | None = None,
    ) -> WorkView:
        request = WorkCreateIn(
            recipe_id=recipe_id,
            preview_sha256=preview_sha256,
            recipe_revision=recipe_revision,
            collection_ids=tuple(collection_ids),
            effective_intent=dict(effective_intent or {}),
        )
        return WorkView.model_validate(
            self._json(
                "POST",
                "/v1/work",
                json=request.model_dump(mode="json", exclude_none=True),
            )
        )

    def get_work(self, work_id: str) -> WorkView:
        return WorkView.model_validate(self._json("GET", f"/v1/work/{quote(work_id, safe='')}"))

    def inspect_work_coordination(self, work_id: str) -> BranchSetEvaluation:
        return BranchSetEvaluation.model_validate(
            self._json(
                "GET",
                f"/v1/work/{quote(work_id, safe='')}/coordination",
            )
        )

    def get_artifact_selection(
        self,
        selection_sha256: str,
        *,
        page: int = 1,
        per_page: int = 100,
        all_items: bool = False,
    ) -> ArtifactSelectionPage:
        return ArtifactSelectionPage.model_validate(
            self._json(
                "GET",
                f"/v1/artifact-selections/{quote(selection_sha256, safe='')}",
                params=_params(page=page, per_page=per_page, **{"all": all_items}),
            )
        )

    def step_work(self, work_id: str) -> WorkView:
        return WorkView.model_validate(
            self._json("POST", f"/v1/work/{quote(work_id, safe='')}/step")
        )

    def retry_work(self, work_id: str) -> WorkView:
        return WorkView.model_validate(
            self._json("POST", f"/v1/work/{quote(work_id, safe='')}/retry")
        )

    def cancel_work(self, work_id: str, *, reason: str | None = None) -> WorkView:
        request = WorkCancelIn(reason=reason)
        return WorkView.model_validate(
            self._json(
                "POST",
                f"/v1/work/{quote(work_id, safe='')}/cancel",
                json=request.model_dump(mode="json", exclude_none=True),
            )
        )

    def preview_workflow(
        self,
        recipe_id: str,
        collection_ids: Sequence[int],
        *,
        recipe_revision: int | None = None,
        effective_intent: Mapping[str, Any] | None = None,
    ) -> WorkflowPreview:
        request = WorkflowPreviewIn(
            recipe_id=recipe_id,
            recipe_revision=recipe_revision,
            collection_ids=tuple(collection_ids),
            effective_intent=dict(effective_intent or {}),
        )
        return WorkflowPreview.model_validate(
            self._json(
                "POST",
                "/v1/workflow-previews",
                json=request.model_dump(mode="json", exclude_none=True),
            )
        )

    def list_evaluations(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        phase: EvaluationPhase | None = None,
        query: str | None = None,
        sort: _EvaluationSort = "updated_at",
        order: _SortOrder = "desc",
        all_items: bool = False,
    ) -> EvaluationPage:
        return EvaluationPage.model_validate(
            self._json(
                "GET",
                "/v1/evaluations",
                params=_params(
                    **{
                        "page": page,
                        "per_page": per_page,
                        "phase": phase,
                        "q": query,
                        "sort": _one_of(
                            sort,
                            frozenset({"updated_at", "phase", "evaluation_id"}),
                            "evaluation sort",
                        ),
                        "order": _one_of(
                            order,
                            frozenset({"asc", "desc"}),
                            "sort order",
                        ),
                        "all": all_items,
                    }
                ),
            )
        )

    def create_evaluation(self, definition: Mapping[str, Any]) -> EvaluationView:
        request = EvaluationDefinition.model_validate(definition)
        return EvaluationView.model_validate(
            self._json(
                "POST",
                "/v1/evaluations",
                json=request.model_dump(mode="json", by_alias=True, exclude_none=True),
            )
        )

    def get_evaluation(self, evaluation_id: str) -> EvaluationView:
        return EvaluationView.model_validate(
            self._json("GET", f"/v1/evaluations/{quote(evaluation_id, safe='')}")
        )

    def step_evaluation(self, evaluation_id: str) -> EvaluationView:
        return EvaluationView.model_validate(
            self._json("POST", f"/v1/evaluations/{quote(evaluation_id, safe='')}/step")
        )

    def cancel_evaluation(
        self,
        evaluation_id: str,
        *,
        reason: str | None = None,
    ) -> EvaluationView:
        request = WorkCancelIn(reason=reason)
        return EvaluationView.model_validate(
            self._json(
                "POST",
                f"/v1/evaluations/{quote(evaluation_id, safe='')}/cancel",
                json=request.model_dump(mode="json", exclude_none=True),
            )
        )

    def retry_evaluation_variant(self, evaluation_id: str, variant_id: str) -> EvaluationView:
        return EvaluationView.model_validate(
            self._json(
                "POST",
                f"/v1/evaluations/{quote(evaluation_id, safe='')}/variants/"
                f"{quote(variant_id, safe='')}/retry",
            )
        )

    def review_evaluation_variant(
        self,
        evaluation_id: str,
        variant_id: str,
        *,
        rating: int | None = None,
        note: str | None = None,
    ) -> EvaluationView:
        request = EvaluationReviewIn(rating=rating, note=note)
        return EvaluationView.model_validate(
            self._json(
                "PUT",
                f"/v1/evaluations/{quote(evaluation_id, safe='')}/variants/"
                f"{quote(variant_id, safe='')}/review",
                json=request.model_dump(mode="json", exclude_none=True),
            )
        )

    def scheduler_status(self) -> SchedulerStatus:
        return SchedulerStatus.model_validate(self._json("GET", "/v1/admin/scheduler"))

    def run_scheduler(
        self,
        *,
        role: SchedulerRole = "combined",
        event_limit: int = 100,
        work_limit: int = 25,
    ) -> SchedulerRun:
        request = SchedulerRunIn(
            role=role,
            event_limit=event_limit,
            work_limit=work_limit,
        )
        return SchedulerRun.model_validate(
            self._json(
                "POST",
                "/v1/admin/scheduler/run",
                json=request.model_dump(mode="json"),
            )
        )

    def _json(
        self,
        method: str,
        path: str,
        *,
        authenticated: bool = True,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if authenticated and not self.token:
            raise Stove0ApiError("STOVE0_TOKEN is required")
        headers = {"Accept": "application/json"}
        if authenticated and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        client = self._persistent_client()
        try:
            response = client.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise Stove0ApiError(f"stove0 request failed: {exc}") from exc
        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = None
            code, message, details = parse_error_payload(
                payload,
                fallback_message=(
                    str(payload.get("detail"))
                    if isinstance(payload, dict) and payload.get("detail")
                    else response.text or f"stove0 returned HTTP {response.status_code}"
                ),
            )
            raise Stove0ApiError(
                message,
                code=code,
                observed_status=response.status_code,
                details=details,
            )
        value = response.json()
        if not isinstance(value, dict):
            raise Stove0ApiError("stove0 returned a non-object JSON response")
        return value

    def _persistent_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                http2=self.http2,
            )
        return self._client


def _boolean_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().casefold()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _params(**values: object) -> dict[str, object]:
    return {name: value for name, value in values.items() if value is not None}


def _positive_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return _positive_float(float(raw), name)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive number of seconds") from exc


def _positive_float(value: float, name: str) -> float:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive number of seconds")
    return value


__all__ = ["Stove0ApiClient", "Stove0ApiError"]

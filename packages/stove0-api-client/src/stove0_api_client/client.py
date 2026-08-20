"""Official stove0 v1 HTTP client."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping, Sequence
from typing import Any, Self
from urllib.parse import quote

import httpx
from http_api_contracts import parse_error_payload, safe_http_base_url
from lifecycle_events import EventPage


class Stove0ApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


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

    def health_live(self) -> dict[str, Any]:
        return self._json("GET", "/health/live", authenticated=False)

    def health_ready(self) -> dict[str, Any]:
        return self._json("GET", "/health/ready", authenticated=False)

    def list_events(self, *, after: str | None = None, limit: int = 100) -> EventPage:
        return EventPage.model_validate(
            self._json("GET", "/v1/events", params=_params(after=after, limit=limit))
        )

    def list_recipes(self) -> dict[str, Any]:
        return self._json("GET", "/v1/recipes")

    def get_recipe(self, recipe_id: str, *, revision: int | None = None) -> dict[str, Any]:
        return self._json(
            "GET",
            f"/v1/recipes/{quote(recipe_id, safe='')}",
            params=_params(revision=revision),
        )

    def list_work(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        phase: str | None = None,
        query: str | None = None,
        sort: str = "updated_at",
        order: str = "desc",
        all_items: bool = False,
    ) -> dict[str, Any]:
        return self._json(
            "GET",
            "/v1/work",
            params=_params(
                **{
                    "page": page,
                    "per_page": per_page,
                    "phase": phase,
                    "q": query,
                    "sort": sort,
                    "order": order,
                    "all": all_items,
                }
            ),
        )

    def create_work(
        self,
        recipe_id: str,
        collection_ids: Sequence[int],
        *,
        recipe_revision: int | None = None,
        effective_intent: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            "/v1/work",
            json={
                "recipe_id": recipe_id,
                "recipe_revision": recipe_revision,
                "collection_ids": list(collection_ids),
                "effective_intent": dict(effective_intent or {}),
            },
        )

    def get_work(self, work_id: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/work/{quote(work_id, safe='')}")

    def step_work(self, work_id: str) -> dict[str, Any]:
        return self._json("POST", f"/v1/work/{quote(work_id, safe='')}/step")

    def retry_work(self, work_id: str) -> dict[str, Any]:
        return self._json("POST", f"/v1/work/{quote(work_id, safe='')}/retry")

    def cancel_work(self, work_id: str, *, reason: str | None = None) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/work/{quote(work_id, safe='')}/cancel",
            json={"reason": reason},
        )

    def preview_workflow(
        self,
        recipe_id: str,
        collection_ids: Sequence[int],
        *,
        recipe_revision: int | None = None,
        effective_intent: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            "/v1/workflow-previews",
            json={
                "recipe_id": recipe_id,
                "recipe_revision": recipe_revision,
                "collection_ids": list(collection_ids),
                "effective_intent": dict(effective_intent or {}),
            },
        )

    def list_evaluations(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        phase: str | None = None,
        query: str | None = None,
        sort: str = "updated_at",
        order: str = "desc",
        all_items: bool = False,
    ) -> dict[str, Any]:
        return self._json(
            "GET",
            "/v1/evaluations",
            params=_params(
                **{
                    "page": page,
                    "per_page": per_page,
                    "phase": phase,
                    "q": query,
                    "sort": sort,
                    "order": order,
                    "all": all_items,
                }
            ),
        )

    def create_evaluation(self, definition: Mapping[str, Any]) -> dict[str, Any]:
        return self._json("POST", "/v1/evaluations", json=dict(definition))

    def get_evaluation(self, evaluation_id: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/evaluations/{quote(evaluation_id, safe='')}")

    def step_evaluation(self, evaluation_id: str) -> dict[str, Any]:
        return self._json("POST", f"/v1/evaluations/{quote(evaluation_id, safe='')}/step")

    def cancel_evaluation(
        self,
        evaluation_id: str,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/evaluations/{quote(evaluation_id, safe='')}/cancel",
            json={"reason": reason},
        )

    def retry_evaluation_variant(self, evaluation_id: str, variant_id: str) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/evaluations/{quote(evaluation_id, safe='')}/variants/"
            f"{quote(variant_id, safe='')}/retry",
        )

    def review_evaluation_variant(
        self,
        evaluation_id: str,
        variant_id: str,
        *,
        rating: int | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        return self._json(
            "PUT",
            f"/v1/evaluations/{quote(evaluation_id, safe='')}/variants/"
            f"{quote(variant_id, safe='')}/review",
            json={"rating": rating, "note": note},
        )

    def scheduler_status(self) -> dict[str, Any]:
        return self._json("GET", "/v1/admin/scheduler")

    def run_scheduler(
        self,
        *,
        role: str = "combined",
        event_limit: int = 100,
        work_limit: int = 25,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            "/v1/admin/scheduler/run",
            json={"role": role, "event_limit": event_limit, "work_limit": work_limit},
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
            _code, message, _details = parse_error_payload(
                payload,
                fallback_message=(
                    str(payload.get("detail"))
                    if isinstance(payload, dict) and payload.get("detail")
                    else response.text or f"stove0 returned HTTP {response.status_code}"
                ),
            )
            raise Stove0ApiError(
                message,
                status_code=response.status_code,
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

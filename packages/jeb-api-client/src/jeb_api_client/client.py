from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from typing import Any, Self
from urllib.parse import quote

import httpx
from jeb_protocol import attempt_state, attempt_watch_finished
from lifecycle_events import EventPage

QueryValue = str | int | float | bool | None


class JebApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "client_error",
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _timeout() -> float:
    raw = os.getenv("JEB_HTTP_TIMEOUT_SECONDS", "300")
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError("JEB_HTTP_TIMEOUT_SECONDS must be a positive number") from exc
    if value <= 0:
        raise ValueError("JEB_HTTP_TIMEOUT_SECONDS must be a positive number")
    return value


class JebApiClient:
    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        self.base_url = (base_url or os.getenv("JEB_BASE_URL") or "http://127.0.0.1:8081").rstrip(
            "/"
        )
        self.token = token or os.getenv("JEB_TOKEN")
        self._client: httpx.Client | None = None

    def _persistent_client(self) -> httpx.Client:
        if self._client is None:
            headers = {"Accept": "application/json"}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            self._client = httpx.Client(
                base_url=self.base_url,
                headers=headers,
                timeout=_timeout(),
                verify=_bool_env("JEB_TLS_VERIFY", True),
                http2=_bool_env("JEB_HTTP2", True),
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, QueryValue] | None = None,
        json: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        response = self._persistent_client().request(
            method,
            path,
            params=params,
            json=json,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise JebApiError(
                f"Jeb returned HTTP {response.status_code} without JSON",
                code="invalid_response",
                status=response.status_code,
            ) from exc
        if response.status_code >= 400:
            error = payload.get("error") if isinstance(payload, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            message = error.get("message") if isinstance(error, dict) else None
            raise JebApiError(
                str(message or f"Jeb returned HTTP {response.status_code}"),
                code=str(code or "http_error"),
                status=response.status_code,
            )
        if not isinstance(payload, dict):
            raise JebApiError(
                "Jeb returned a non-object JSON response",
                code="invalid_response",
                status=response.status_code,
            )
        return payload

    def get_status(self, *, include_backlog: bool = True) -> dict[str, Any]:
        return self._json(
            "GET",
            "/v1/status",
            params={"include_backlog": str(include_backlog).lower()},
        )

    def list_lifecycle_events(
        self,
        *,
        after: str | None = None,
        limit: int = 100,
    ) -> EventPage:
        return EventPage.model_validate(
            self._json(
                "GET",
                "/v1/events",
                params={"after": after or "0", "limit": limit},
            )
        )

    def list_attempts(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        sort: str = "updated_at",
        order: str = "desc",
        resolution: str = "unresolved",
        state: str | None = None,
        source: str | None = None,
        target: str | None = None,
        query: str | None = None,
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, QueryValue] = {
            "page": page,
            "per_page": per_page,
            "sort": sort,
            "order": order,
            "resolution": resolution,
        }
        for key, value in {
            "state": state,
            "source": source,
            "target": target,
            "q": query,
        }.items():
            if value is not None:
                params[key] = value
        if all_items:
            params["all"] = True
        return self._json("GET", "/v1/attempts", params=params)

    def get_attempt(self, attempt_id: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/attempts/{quote(attempt_id, safe='')}")

    def cancel_attempt(self, attempt_id: str) -> dict[str, Any]:
        return self._json("DELETE", f"/v1/attempts/{quote(attempt_id, safe='')}")

    def wait_for_attempt(
        self,
        attempt_id: str,
        *,
        interval: float = 10.0,
        on_update: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        if interval <= 0:
            raise ValueError("interval must be positive")
        last_state: str | None = None
        while True:
            try:
                attempt = self.get_attempt(attempt_id)
            except httpx.TransportError:
                time.sleep(interval)
                continue
            state = attempt_state(attempt)
            if not state:
                raise JebApiError("Jeb attempt response is missing state")
            if on_update is not None and state != last_state:
                on_update(attempt)
            if attempt_watch_finished(attempt):
                return attempt
            last_state = state
            time.sleep(interval)

    def check_config(self) -> dict[str, Any]:
        return self._json("GET", "/v1/config/check")

    def run_once(self) -> dict[str, Any]:
        return self._json("POST", "/v1/once")

    def list_operations(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        sort: str = "started_at",
        order: str = "desc",
        state: str | None = None,
        query: str | None = None,
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, QueryValue] = {
            "page": page,
            "per_page": per_page,
            "sort": sort,
            "order": order,
        }
        if state is not None:
            params["state"] = state
        if query is not None:
            params["q"] = query
        if all_items:
            params["all"] = True
        return self._json("GET", "/v1/operations", params=params)

    def get_operation(self, operation_id: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/operations/{quote(operation_id, safe='')}")

    def wait_for_operation(
        self,
        operation_id: str,
        *,
        interval: float = 1.0,
    ) -> dict[str, Any]:
        if interval <= 0:
            raise ValueError("interval must be positive")
        while True:
            try:
                operation = self.get_operation(operation_id)
            except httpx.TransportError:
                time.sleep(interval)
                continue
            if operation.get("state") in {"succeeded", "failed"}:
                return operation
            time.sleep(interval)

    def archive_source_now(
        self,
        *,
        source: str,
        process: bool = True,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            "/v1/archive-now",
            json={"source": source, "process": process, "dry_run": dry_run},
        )

    def list_sources(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        sort: str = "id",
        order: str = "asc",
        query: str | None = None,
        enabled: bool | None = None,
        adapter: str | None = None,
        target: str | None = None,
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, QueryValue] = {
            "page": page,
            "per_page": per_page,
            "sort": sort,
            "order": order,
        }
        for key, value in {
            "q": query,
            "enabled": None if enabled is None else str(enabled).lower(),
            "adapter": adapter,
            "target": target,
        }.items():
            if value is not None:
                params[key] = value
        if all_items:
            params["all"] = True
        return self._json("GET", "/v1/sources", params=params)

    def get_source(self, source_id: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/sources/{quote(source_id, safe='')}")

    def create_source(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._json("POST", "/v1/sources", json=payload)

    def update_source(
        self,
        source_id: str,
        changes: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._json(
            "PATCH",
            f"/v1/sources/{quote(source_id, safe='')}",
            json=changes,
        )

    def enable_source(self, source_id: str) -> dict[str, Any]:
        return self._json("POST", f"/v1/sources/{quote(source_id, safe='')}/enable")

    def disable_source(self, source_id: str) -> dict[str, Any]:
        return self._json("POST", f"/v1/sources/{quote(source_id, safe='')}/disable")

    def rotate_source_credential(
        self,
        source_id: str,
        *,
        credential: str | None = None,
    ) -> dict[str, Any]:
        payload = {} if credential is None else {"credential": credential}
        return self._json(
            "POST",
            f"/v1/sources/{quote(source_id, safe='')}/credential",
            json=payload,
        )

    def plan_source_removal(
        self,
        source_id: str,
        *,
        purge: bool,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/sources/{quote(source_id, safe='')}/removal-plan",
            json={"purge": purge},
        )

    def remove_source(self, source_id: str, *, challenge: str) -> dict[str, Any]:
        return self._json(
            "DELETE",
            f"/v1/sources/{quote(source_id, safe='')}",
            json={"challenge": challenge},
        )


__all__ = ["JebApiClient", "JebApiError"]

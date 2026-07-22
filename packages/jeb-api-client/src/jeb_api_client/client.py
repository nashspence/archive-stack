from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Self
from urllib.parse import quote

import httpx
from lifecycle_events import EventPage

QueryValue = str | int | float | bool | None


class JebApiError(RuntimeError):
    pass


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
            raise JebApiError(f"Jeb returned HTTP {response.status_code} without JSON") from exc
        if response.status_code >= 400:
            error = payload.get("error") if isinstance(payload, dict) else None
            message = error.get("message") if isinstance(error, dict) else None
            raise JebApiError(str(message or f"Jeb returned HTTP {response.status_code}"))
        if not isinstance(payload, dict):
            raise JebApiError("Jeb returned a non-object JSON response")
        return payload

    def status(self, *, include_backlog: bool = True) -> dict[str, Any]:
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
        terminal: str = "active",
        state: str | None = None,
        source: str | None = None,
        collection_slug: str | None = None,
        target: str | None = None,
        query: str | None = None,
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, QueryValue] = {
            "page": page,
            "per_page": per_page,
            "sort": sort,
            "order": order,
            "terminal": terminal,
        }
        for key, value in {
            "state": state,
            "source": source,
            "collection_slug": collection_slug,
            "target": target,
            "q": query,
        }.items():
            if value is not None:
                params[key] = value
        if all_items:
            params["all"] = True
        return self._json("GET", "/v1/attempts", params=params)

    def check_config(self) -> dict[str, Any]:
        return self._json("GET", "/v1/config/check")

    def run_once(self) -> dict[str, Any]:
        return self._json("POST", "/v1/once")

    def archive_now(
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

    def add_source(self, payload: Mapping[str, Any]) -> dict[str, Any]:
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

    def set_source_enabled(self, source_id: str, *, enabled: bool) -> dict[str, Any]:
        action = "enable" if enabled else "disable"
        return self._json("POST", f"/v1/sources/{quote(source_id, safe='')}/{action}")

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

from __future__ import annotations

import os
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from riverhog_core.domain.errors import BadRequest, InvalidState, RiverhogError, ServiceUnavailable

router = APIRouter(tags=["jeb"])
DEFAULT_JEB_SERVICE_URL = "http://riverhog-jeb:8081"
DEFAULT_JEB_SERVICE_TIMEOUT_SECONDS = 300.0


class JebArchiveNowRequest(BaseModel):
    account: str = Field(min_length=1)
    process: bool = True


def _jeb_service_url() -> str:
    return os.getenv("RIVERHOG_JEB_URL", DEFAULT_JEB_SERVICE_URL).rstrip("/")


def _jeb_service_timeout() -> float:
    raw = os.getenv("RIVERHOG_JEB_TIMEOUT_SECONDS", str(DEFAULT_JEB_SERVICE_TIMEOUT_SECONDS))
    try:
        value = float(raw)
    except ValueError as exc:
        raise BadRequest("RIVERHOG_JEB_TIMEOUT_SECONDS must be a positive number") from exc
    if value <= 0:
        raise BadRequest("RIVERHOG_JEB_TIMEOUT_SECONDS must be a positive number")
    return value


def _error_from_payload(payload: object, fallback: str) -> RiverhogError:
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    if not isinstance(error, dict):
        error = {}
    code = str(error.get("code") or "service_unavailable")
    message = str(error.get("message") or fallback)
    if code == "bad_request":
        return BadRequest(message)
    if code == "invalid_state":
        return InvalidState(message)
    return ServiceUnavailable(message)


def _request_jeb_service(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        with httpx.Client(base_url=_jeb_service_url(), timeout=_jeb_service_timeout()) as client:
            response = client.request(method, path, params=params, json=json_payload)
    except httpx.TransportError as exc:
        raise ServiceUnavailable(f"Jeb service is unavailable: {exc}") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise ServiceUnavailable("Jeb service returned non-JSON response") from exc
    if response.status_code >= 400:
        raise _error_from_payload(payload, f"Jeb service returned HTTP {response.status_code}")
    if not isinstance(payload, dict):
        raise ServiceUnavailable("Jeb service returned a non-object JSON payload")
    return payload


@router.get("/jeb/status")
def get_jeb_status(include_backlog: bool = Query(True)) -> dict[str, Any]:
    return _request_jeb_service(
        "GET",
        "/v1/jeb/status",
        params={"include_backlog": str(include_backlog).lower()},
    )


@router.get("/jeb/batches")
def list_jeb_batches(
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=500)] = 25,
    sort: str = Query("updated_at"),
    order: Literal["asc", "desc"] = Query("desc"),
    terminal: Literal["active", "terminal", "all"] = Query("active"),
    state: str | None = Query(None),
    account: str | None = Query(None),
    collection: str | None = Query(None),
    target: str | None = Query(None),
    q: str | None = Query(None),
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "page": page,
        "per_page": per_page,
        "sort": sort,
        "order": order,
        "terminal": terminal,
    }
    for key, value in {
        "state": state,
        "account": account,
        "collection": collection,
        "target": target,
        "q": q,
    }.items():
        if value is not None:
            params[key] = value
    return _request_jeb_service("GET", "/v1/jeb/batches", params=params)


@router.get("/jeb/config/check")
def check_jeb_config() -> dict[str, Any]:
    return _request_jeb_service("GET", "/v1/jeb/config/check")


@router.post("/jeb/once", status_code=202)
def run_jeb_once() -> dict[str, Any]:
    return _request_jeb_service("POST", "/v1/jeb/once")


@router.post("/jeb/archive-now", status_code=202)
def archive_jeb_now(request: JebArchiveNowRequest) -> dict[str, Any]:
    return _request_jeb_service(
        "POST",
        "/v1/jeb/archive-now",
        json_payload=request.model_dump(),
    )

from __future__ import annotations

import os
from datetime import timedelta
from typing import Annotated, Any, Literal
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field

from jeb.listing import MAX_LIST_PAGE_SIZE
from riverhog_core.domain.errors import BadRequest, InvalidState, RiverhogError, ServiceUnavailable
from riverhog_core.runtime_config import parse_duration

router = APIRouter(tags=["jeb"])
DEFAULT_JEB_SERVICE_URL = "http://riverhog-jeb:8081"
DEFAULT_JEB_TIMEOUT = timedelta(minutes=5)


class JebArchiveNowRequest(BaseModel):
    source: str = Field(min_length=1)
    process: bool = True
    dry_run: bool = False


class JebSourceAddRequest(BaseModel):
    id: str = Field(min_length=1)
    adapters: list[str] = Field(min_length=1)
    policy: dict[str, Any]
    credential: str | None = None
    enabled: bool = True
    stable_seconds: int = Field(600, ge=0)
    include_extensions: list[str] | None = None
    collection_slug: str | None = None
    target: str = "munchy"
    notify: dict[str, Any] | None = None
    threshold_bytes: int = Field(0, ge=0)
    cleanup: Literal["never", "after_target_success"] = "after_target_success"
    cadence: Literal["weekly", "monthly", "seasonal", "manual"] = "weekly"
    weekday: int = Field(0, ge=0, le=6)
    hour: int = Field(3, ge=0, le=23)
    minute: int = Field(0, ge=0, le=59)


class JebSourceCredentialRequest(BaseModel):
    credential: str | None = None


class JebSourceRemovalPlanRequest(BaseModel):
    purge: bool = False


class JebSourceRemoveRequest(BaseModel):
    challenge: str = Field(min_length=1)


def _jeb_service_url() -> str:
    return os.getenv("RIVERHOG_JEB_URL", DEFAULT_JEB_SERVICE_URL).rstrip("/")


def _jeb_service_timeout() -> float:
    raw = os.getenv("RIVERHOG_JEB_TIMEOUT", "5m")
    try:
        value = parse_duration(raw).total_seconds()
    except ValueError as exc:
        raise BadRequest("RIVERHOG_JEB_TIMEOUT must be a positive duration") from exc
    if value <= 0:
        raise BadRequest("RIVERHOG_JEB_TIMEOUT must be a positive duration")
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


@router.get("/jeb/sources")
def list_jeb_sources(
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=MAX_LIST_PAGE_SIZE)] = 25,
    sort: str = Query("id"),
    order: Literal["asc", "desc"] = Query("asc"),
    q: str | None = Query(None),
    enabled: bool | None = Query(None),
    adapter: Literal["ftp", "tus"] | None = Query(None),
    target: str | None = Query(None),
    all_items: Annotated[bool, Query(alias="all")] = False,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "page": page,
        "per_page": per_page,
        "sort": sort,
        "order": order,
    }
    for key, value in {
        "q": q,
        "enabled": None if enabled is None else str(enabled).lower(),
        "adapter": adapter,
        "target": target,
    }.items():
        if value is not None:
            params[key] = value
    if all_items:
        params["all"] = "true"
    return _request_jeb_service("GET", "/v1/jeb/sources", params=params)


@router.post("/jeb/sources", status_code=201)
def add_jeb_source(request: JebSourceAddRequest) -> dict[str, Any]:
    return _request_jeb_service(
        "POST",
        "/v1/jeb/sources",
        json_payload=request.model_dump(exclude_none=True),
    )


@router.get("/jeb/sources/{source_id}")
def get_jeb_source(source_id: str) -> dict[str, Any]:
    return _request_jeb_service(
        "GET",
        f"/v1/jeb/sources/{quote(source_id, safe='')}",
    )


@router.patch("/jeb/sources/{source_id}")
def update_jeb_source(source_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    return _request_jeb_service(
        "PATCH",
        f"/v1/jeb/sources/{quote(source_id, safe='')}",
        json_payload=changes,
    )


@router.post("/jeb/sources/{source_id}/enable")
def enable_jeb_source(source_id: str) -> dict[str, Any]:
    return _request_jeb_service(
        "POST",
        f"/v1/jeb/sources/{quote(source_id, safe='')}/enable",
    )


@router.post("/jeb/sources/{source_id}/disable")
def disable_jeb_source(source_id: str) -> dict[str, Any]:
    return _request_jeb_service(
        "POST",
        f"/v1/jeb/sources/{quote(source_id, safe='')}/disable",
    )


@router.post("/jeb/sources/{source_id}/credential")
def rotate_jeb_source_credential(
    source_id: str,
    request: JebSourceCredentialRequest,
) -> dict[str, Any]:
    return _request_jeb_service(
        "POST",
        f"/v1/jeb/sources/{quote(source_id, safe='')}/credential",
        json_payload=request.model_dump(exclude_none=True),
    )


@router.post("/jeb/sources/{source_id}/removal-plan")
def plan_jeb_source_removal(
    source_id: str,
    request: JebSourceRemovalPlanRequest,
) -> dict[str, Any]:
    return _request_jeb_service(
        "POST",
        f"/v1/jeb/sources/{quote(source_id, safe='')}/removal-plan",
        json_payload=request.model_dump(),
    )


@router.delete("/jeb/sources/{source_id}")
def remove_jeb_source(source_id: str, request: JebSourceRemoveRequest) -> dict[str, Any]:
    return _request_jeb_service(
        "DELETE",
        f"/v1/jeb/sources/{quote(source_id, safe='')}",
        json_payload=request.model_dump(),
    )


@router.get("/jeb/attempts")
def list_jeb_attempts(
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=MAX_LIST_PAGE_SIZE)] = 25,
    sort: str = Query("updated_at"),
    order: Literal["asc", "desc"] = Query("desc"),
    terminal: Literal["active", "terminal", "all"] = Query("active"),
    state: str | None = Query(None),
    source: str | None = Query(None),
    collection_slug: str | None = Query(None),
    target: str | None = Query(None),
    q: str | None = Query(None),
    all_items: Annotated[bool, Query(alias="all")] = False,
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
        "source": source,
        "collection_slug": collection_slug,
        "target": target,
        "q": q,
    }.items():
        if value is not None:
            params[key] = value
    if all_items:
        params["all"] = "true"
    return _request_jeb_service("GET", "/v1/jeb/attempts", params=params)


@router.get("/jeb/config/check")
def check_jeb_config() -> dict[str, Any]:
    return _request_jeb_service("GET", "/v1/jeb/config/check")


@router.post("/jeb/once", status_code=202)
def run_jeb_once() -> dict[str, Any]:
    return _request_jeb_service("POST", "/v1/jeb/once")


@router.post(
    "/jeb/archive-now",
    status_code=202,
    responses={
        200: {
            "description": "Dry-run archive plan returned without mutating Jeb state",
            "content": {"application/json": {"schema": {"type": "object"}}},
        }
    },
)
def archive_jeb_now(request: JebArchiveNowRequest, response: Response) -> dict[str, Any]:
    payload = _request_jeb_service(
        "POST",
        "/v1/jeb/archive-now",
        json_payload=request.model_dump(),
    )
    if request.dry_run:
        response.status_code = 200
    return payload

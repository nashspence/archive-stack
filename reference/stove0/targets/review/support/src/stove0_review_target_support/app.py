"""Shared HTTP and sampler-configuration support for exact review targets."""

from __future__ import annotations

import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Protocol

from fastapi import Depends, FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.security import HTTPBearer
from http_api_contracts import ErrorResponse, HealthResponse, error_payload, operation_openapi
from pydantic import BaseModel, ConfigDict, Field, field_validator
from stove0_review_sampler_client import ReviewSamplerClient
from stove0_target_support import TARGET_HTTP_OPERATIONS, TargetHttpBinding

from stove0_review_target_support.target import SamplerRegistration

_PUBLIC_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_bearer = HTTPBearer(auto_error=False)


class ReviewTarget(Protocol):
    def close(self) -> None: ...

    def readiness(self) -> dict[str, str]: ...


class SamplerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,118}[a-z0-9])?$")
    base_url: str = Field(min_length=1, max_length=2048)
    token_file: Path
    allow_insecure_http: bool = False
    descriptor_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    image_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("token_file")
    @classmethod
    def absolute_token_file(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("sampler token file path must be absolute")
        return value


class ReviewTargetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    samplers: tuple[SamplerConfig, ...] = Field(min_length=1)

    @field_validator("samplers")
    @classmethod
    def canonical_samplers(cls, value: tuple[SamplerConfig, ...]) -> tuple[SamplerConfig, ...]:
        ids = [item.id for item in value]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("samplers must be unique and ordered")
        return value


def load_sampler_registrations(path: Path) -> tuple[SamplerRegistration, ...]:
    return parse_sampler_registrations(path.read_text(encoding="utf-8"))


def parse_sampler_registrations(document: str) -> tuple[SamplerRegistration, ...]:
    config = ReviewTargetConfig.model_validate_json(document)
    registrations = []
    for item in config.samplers:
        token = item.token_file.read_text(encoding="utf-8").strip()
        if not token:
            raise ValueError(f"sampler token is empty: {item.id}")
        registrations.append(
            SamplerRegistration(
                id=item.id,
                client=ReviewSamplerClient(
                    item.base_url,
                    token,
                    allow_insecure_http=item.allow_insecure_http,
                ),
                descriptor_sha256=item.descriptor_sha256,
                image_digest=item.image_digest,
            )
        )
    return tuple(registrations)


def create_target_app(
    *,
    service: str,
    title: str,
    token: str,
    target: ReviewTarget,
) -> FastAPI:
    credential = token.strip()
    if not credential:
        raise ValueError("review target token must be nonempty")
    binding = TargetHttpBinding(target)  # type: ignore[arg-type]

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            target.close()

    app = FastAPI(title=title, version="1", lifespan=lifespan, openapi_url="/v1/openapi.json")

    @app.get("/health/live", response_model=HealthResponse, tags=["health"])
    def live() -> dict[str, str]:
        return {"service": service, "status": "ok"}

    @app.get(
        "/health/ready",
        response_model=HealthResponse,
        responses={503: {"model": ErrorResponse}},
        tags=["health"],
    )
    def ready() -> Response:
        try:
            target.readiness()
        except Exception:
            return _error(503, "service_unavailable", "configured review samplers are not ready")
        return Response(
            content=json.dumps({"service": service, "status": "ok"}, separators=(",", ":")),
            media_type="application/json",
        )

    async def dispatch(request: Request) -> Response:
        scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
        if scheme.casefold() != "bearer" or not secrets.compare_digest(supplied, credential):
            return _error(401, "unauthorized", "Bearer credential is not authorized")
        result = await run_in_threadpool(
            binding.handle, request.method, request.url.path, await request.body()
        )
        return Response(
            content=result.body, status_code=result.status, headers=dict(result.headers)
        )

    methods_by_path: dict[str, set[str]] = {}
    for operation in TARGET_HTTP_OPERATIONS:
        methods_by_path.setdefault(operation.path, set()).add(operation.method)
        app.add_api_route(
            operation.path,
            dispatch,
            methods=[operation.method],
            dependencies=[Depends(_bearer)],
            tags=["target"],
            **operation_openapi(operation),
        )
    for path, supported in methods_by_path.items():
        app.add_api_route(
            path,
            dispatch,
            methods=sorted(_PUBLIC_METHODS - supported),
            include_in_schema=False,
        )
    return app


def _error(status: int, code: str, message: str) -> Response:
    return Response(
        content=json.dumps(error_payload(code=code, message=message), separators=(",", ":")),
        status_code=status,
        media_type="application/json",
    )


__all__ = [
    "ReviewTargetConfig",
    "SamplerConfig",
    "create_target_app",
    "load_sampler_registrations",
    "parse_sampler_registrations",
]

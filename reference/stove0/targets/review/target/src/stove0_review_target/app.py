"""Authenticated one-role review target service."""

from __future__ import annotations

import argparse
import contextlib
import importlib.metadata
import json
import os
import secrets
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, cast

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from http_api_contracts import error_payload
from pydantic import BaseModel, ConfigDict, Field, field_validator
from stove0_review_sampler_client import ReviewSamplerClient
from stove0_target_support import TargetHttpBinding, terminal_state_retention_seconds

from stove0_review_target.target import (
    RcloneReviewDestination,
    ReviewTargetService,
    SamplerRegistration,
)

SERVICE = "stove0-review-target"


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


def _sampler_registrations() -> tuple[SamplerRegistration, ...]:
    direct = os.getenv("STOVE0_REVIEW_TARGET_SAMPLERS_JSON")
    path = os.getenv("STOVE0_REVIEW_TARGET_SAMPLERS_JSON_FILE")
    if bool(direct) == bool(path):
        raise ValueError("set exactly one review target sampler configuration source")
    if direct is not None:
        return parse_sampler_registrations(direct)
    return load_sampler_registrations(Path(str(path)))


def create_app(*, token: str, target: ReviewTargetService) -> FastAPI:
    credential = token.strip()
    if not credential:
        raise ValueError("review target token must be nonempty")
    binding = TargetHttpBinding(target)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            target.close()

    app = FastAPI(
        title="Stove0 review target", version="1", lifespan=lifespan, openapi_url="/v1/openapi.json"
    )

    @app.get("/health/live", tags=["health"])
    def live() -> dict[str, str]:
        return {"service": SERVICE, "status": "ok"}

    @app.get("/health/ready", tags=["health"])
    def ready() -> Response:
        try:
            target.readiness()
        except Exception:
            return _error(503, "service_unavailable", "configured review samplers are not ready")
        return Response(
            content=b'{"service":"stove0-review-target","status":"ok"}',
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

    for path in ("/v1/target", "/v1/preflight", "/v1/jobs/{job_id}", "/v1/jobs/{job_id}/cancel"):
        app.add_api_route(
            path,
            dispatch,
            methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            tags=["target"],
        )
    return app


def _error(status: int, code: str, message: str) -> Response:
    return Response(
        content=json.dumps(error_payload(code=code, message=message), separators=(",", ":")),
        status_code=status,
        media_type="application/json",
    )


def _secret() -> str:
    direct = os.getenv("STOVE0_REVIEW_TARGET_TOKEN")
    path = os.getenv("STOVE0_REVIEW_TARGET_TOKEN_FILE")
    if bool(direct) == bool(path):
        raise ValueError("set exactly one review target token source")
    value = direct if direct is not None else Path(str(path)).read_text(encoding="utf-8")
    if not value.strip():
        raise ValueError("review target token must be nonempty")
    return value.strip()


def _image_digest() -> str:
    value = os.getenv("STOVE0_REVIEW_TARGET_IMAGE_DIGEST", "").strip()
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("STOVE0_REVIEW_TARGET_IMAGE_DIGEST must be a lowercase SHA-256")
    return value


def _mode() -> Literal["collection", "rclone-effect"]:
    value = os.getenv("STOVE0_REVIEW_TARGET_MODE", "").strip()
    if value not in {"collection", "rclone-effect"}:
        raise ValueError("STOVE0_REVIEW_TARGET_MODE must be collection or rclone-effect")
    return cast(Literal["collection", "rclone-effect"], value)


def _effect_destination(
    mode: Literal["collection", "rclone-effect"],
) -> RcloneReviewDestination | None:
    identity = os.getenv("STOVE0_REVIEW_TARGET_DESTINATION_IDENTITY", "").strip()
    remote = os.getenv("STOVE0_REVIEW_TARGET_RCLONE_REMOTE", "").strip()
    config = os.getenv("STOVE0_REVIEW_TARGET_RCLONE_CONFIG_FILE", "").strip()
    if mode == "collection":
        if identity or remote or config:
            raise ValueError("collection review mode cannot configure an rclone destination")
        return None
    if not identity or not remote:
        raise ValueError("rclone-effect review mode requires destination identity and remote")
    timeout = int(os.getenv("STOVE0_REVIEW_TARGET_RCLONE_TIMEOUT_SECONDS", "86400"))
    return RcloneReviewDestination(
        identity=identity,
        remote=remote,
        config_path=Path(config) if config else None,
        executable=os.getenv("STOVE0_REVIEW_TARGET_RCLONE_BIN", "rclone").strip(),
        timeout_seconds=timeout,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=SERVICE)
    parser.add_argument("--version", action="version", version=importlib.metadata.version(SERVICE))
    parser.add_argument("--host", default=os.getenv("STOVE0_REVIEW_TARGET_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("STOVE0_REVIEW_TARGET_PORT", "8080"))
    )
    args = parser.parse_args(argv)
    mode = _mode()
    target = ReviewTargetService(
        state_root=Path(
            os.getenv("STOVE0_REVIEW_TARGET_STATE_ROOT", "/var/lib/stove0-review-target")
        ),
        workspace_root=Path(
            os.getenv("STOVE0_REVIEW_TARGET_WORKSPACE", "/run/stove0-review-target")
        ),
        samplers=_sampler_registrations(),
        source_revision=os.getenv("STOVE0_REVIEW_TARGET_SOURCE_REVISION", "unknown"),
        image_digest=_image_digest(),
        mode=mode,
        destination=_effect_destination(mode),
        terminal_state_retention_seconds=terminal_state_retention_seconds(),
    )
    token = _secret()
    with contextlib.suppress(KeyError):
        os.environ.pop("STOVE0_REVIEW_TARGET_TOKEN")
    uvicorn.run(create_app(token=token, target=target), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ReviewTargetConfig",
    "SamplerConfig",
    "create_app",
    "load_sampler_registrations",
    "main",
    "parse_sampler_registrations",
]

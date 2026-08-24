"""Authenticated HTTP process for the NVENC AV1 plus Opus review-sampler role."""

from __future__ import annotations

import argparse
import contextlib
import importlib.metadata
import json
import os
import secrets
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import uvicorn
from fastapi import Depends, FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.security import HTTPBearer
from http_api_contracts import ErrorResponse, HealthResponse, error_payload, operation_openapi
from stove0_review_sampler_support import (
    SAMPLER_HTTP_OPERATIONS,
    SamplerHttpBinding,
    SamplerHttpResponse,
)

from stove0_nvenc_av1_opus_review_sampler.sampler import NvencAv1OpusReviewSampler

SERVICE = "stove0-nvenc-av1-opus-review-sampler"
_PUBLIC_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_bearer = HTTPBearer(auto_error=False)


def create_app(*, token: str, sampler: NvencAv1OpusReviewSampler) -> FastAPI:
    credential = token.strip()
    if not credential:
        raise ValueError(f"{SERVICE} token must be nonempty")
    binding = SamplerHttpBinding(sampler)
    app = FastAPI(title=SERVICE, version="1", openapi_url="/v1/openapi.json")

    @app.get("/health/live", response_model=HealthResponse, tags=["health"])
    def live() -> dict[str, str]:
        return {"service": SERVICE, "status": "ok"}

    @app.get(
        "/health/ready",
        response_model=HealthResponse,
        responses={503: {"model": ErrorResponse}},
        tags=["health"],
    )
    def ready() -> Response:
        result = subprocess.run(
            [sampler.ffmpeg, "-hide_banner", "-encoders"],
            check=False,
            capture_output=True,
            timeout=30,
        )
        if result.returncode or b"av1_nvenc" not in result.stdout:
            return _error(503, "service_unavailable", "AV1 NVENC is not ready")
        return Response(
            content=json.dumps({"service": SERVICE, "status": "ok"}, separators=(",", ":")),
            media_type="application/json",
        )

    async def dispatch(request: Request) -> Response:
        scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
        if scheme.casefold() != "bearer" or not secrets.compare_digest(supplied, credential):
            return _error(401, "unauthorized", "Bearer credential is not authorized")
        result = cast(
            SamplerHttpResponse,
            await run_in_threadpool(
                binding.handle, request.method, request.url.path, await request.body()
            ),
        )
        return Response(
            content=result.body, status_code=result.status, headers=dict(result.headers)
        )

    methods_by_path: dict[str, set[str]] = {}
    for operation in SAMPLER_HTTP_OPERATIONS:
        methods_by_path.setdefault(operation.path, set()).add(operation.method)
        app.add_api_route(
            operation.path,
            dispatch,
            methods=[operation.method],
            dependencies=[Depends(_bearer)],
            tags=["review-sampler"],
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


def _secret(prefix: str) -> str:
    direct = os.getenv(f"{prefix}_TOKEN")
    path = os.getenv(f"{prefix}_TOKEN_FILE")
    if bool(direct) == bool(path):
        raise ValueError(f"set exactly one {prefix} token source")
    value = direct if direct is not None else Path(str(path)).read_text(encoding="utf-8")
    if not value.strip():
        raise ValueError(f"{prefix} token must be nonempty")
    return value.strip()


def _image_digest(prefix: str) -> str:
    value = os.getenv(f"{prefix}_IMAGE_DIGEST", "").strip()
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{prefix}_IMAGE_DIGEST must be a lowercase SHA-256")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    prefix = "STOVE0_NVENC_AV1_OPUS_REVIEW_SAMPLER"
    parser = argparse.ArgumentParser(prog=SERVICE)
    parser.add_argument("--version", action="version", version=importlib.metadata.version(SERVICE))
    parser.add_argument("--host", default=os.getenv(f"{prefix}_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv(f"{prefix}_PORT", "8080")))
    args = parser.parse_args(argv)
    sampler = NvencAv1OpusReviewSampler(
        workspace_root=Path(os.getenv(f"{prefix}_WORKSPACE", "/run/stove0-review-target")),
        ffmpeg=os.getenv("STOVE0_FFMPEG_BIN", "ffmpeg"),
        source_revision=os.getenv(f"{prefix}_SOURCE_REVISION", "unknown"),
        image_digest=_image_digest(prefix),
    )
    token = _secret(prefix)
    with contextlib.suppress(KeyError):
        os.environ.pop(f"{prefix}_TOKEN")
    uvicorn.run(create_app(token=token, sampler=sampler), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["create_app", "main"]

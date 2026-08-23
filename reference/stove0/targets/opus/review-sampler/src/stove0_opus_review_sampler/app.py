"""Authenticated HTTP process for the Opus review-sampler role."""

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
from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from http_api_contracts import error_payload
from stove0_review_sampler_support import SamplerHttpBinding, SamplerHttpResponse

from stove0_opus_review_sampler.sampler import OpusReviewSampler

SERVICE = "stove0-opus-review-sampler"


def create_app(*, token: str, sampler: OpusReviewSampler) -> FastAPI:
    credential = token.strip()
    if not credential:
        raise ValueError(f"{SERVICE} token must be nonempty")
    binding = SamplerHttpBinding(sampler)
    app = FastAPI(title=SERVICE, version="1", openapi_url="/v1/openapi.json")

    @app.get("/health/live", tags=["health"])
    def live() -> dict[str, str]:
        return {"service": SERVICE, "status": "ok"}

    @app.get("/health/ready", tags=["health"])
    def ready() -> Response:
        result = subprocess.run(
            [sampler.ffmpeg, "-version"], check=False, capture_output=True, timeout=15
        )
        if result.returncode:
            return _error(503, "service_unavailable", "FFmpeg is not ready")
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

    for path in ("/v1/sampler", "/v1/sample"):
        app.add_api_route(path, dispatch, methods=["GET", "POST"], tags=["review-sampler"])
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
    prefix = "STOVE0_OPUS_REVIEW_SAMPLER"
    parser = argparse.ArgumentParser(prog=SERVICE)
    parser.add_argument("--version", action="version", version=importlib.metadata.version(SERVICE))
    parser.add_argument("--host", default=os.getenv(f"{prefix}_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv(f"{prefix}_PORT", "8080")))
    args = parser.parse_args(argv)
    sampler = OpusReviewSampler(
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

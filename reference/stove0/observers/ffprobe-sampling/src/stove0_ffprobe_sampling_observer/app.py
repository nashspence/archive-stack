"""Authenticated one-role ASGI service for the FFprobe observer."""

from __future__ import annotations

import argparse
import contextlib
import importlib.metadata
import os
import secrets
import subprocess
from collections.abc import Sequence
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.security import HTTPBearer
from http_api_contracts import ErrorResponse, HealthResponse, error_payload, operation_openapi
from stove0_observer_support import OBSERVER_HTTP_OPERATIONS, ObserverHttpBinding

from stove0_ffprobe_sampling_observer.observer import FfprobeSamplingObserver

SERVICE = "stove0-ffprobe-sampling-observer"
_PUBLIC_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_bearer = HTTPBearer(auto_error=False)


def create_app(*, token: str, observer: FfprobeSamplingObserver) -> FastAPI:
    credential = token.strip()
    if not credential:
        raise ValueError("FFprobe observer token must be nonempty")
    binding = ObserverHttpBinding(observer)
    app = FastAPI(
        title="Stove0 FFprobe sampling observer", version="1", openapi_url="/v1/openapi.json"
    )

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
            [observer.ffprobe, "-version"], check=False, capture_output=True, timeout=15
        )
        if result.returncode:
            return _error(503, "service_unavailable", "FFprobe is not ready")
        return Response(
            content=b'{"service":"stove0-ffprobe-sampling-observer","status":"ok"}',
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
    for operation in OBSERVER_HTTP_OPERATIONS:
        methods_by_path.setdefault(operation.path, set()).add(operation.method)
        app.add_api_route(
            operation.path,
            dispatch,
            methods=[operation.method],
            dependencies=[Depends(_bearer)],
            tags=["observer"],
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
    import json

    return Response(
        content=json.dumps(error_payload(code=code, message=message), separators=(",", ":")),
        status_code=status,
        media_type="application/json",
    )


def _secret() -> str:
    direct = os.getenv("STOVE0_FFPROBE_SAMPLING_OBSERVER_TOKEN")
    path = os.getenv("STOVE0_FFPROBE_SAMPLING_OBSERVER_TOKEN_FILE")
    if bool(direct) == bool(path):
        raise ValueError("set exactly one FFprobe observer token source")
    value = direct if direct is not None else Path(str(path)).read_text(encoding="utf-8")
    if not value.strip():
        raise ValueError("FFprobe observer token must be nonempty")
    return value.strip()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=SERVICE)
    parser.add_argument("--version", action="version", version=importlib.metadata.version(SERVICE))
    parser.add_argument(
        "--host", default=os.getenv("STOVE0_FFPROBE_SAMPLING_OBSERVER_HOST", "127.0.0.1")
    )
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("STOVE0_FFPROBE_SAMPLING_OBSERVER_PORT", "8080"))
    )
    args = parser.parse_args(argv)
    observer = FfprobeSamplingObserver(
        ffprobe=os.getenv("STOVE0_FFPROBE_BIN", "ffprobe"),
        workspace_root=Path(
            os.getenv(
                "STOVE0_FFPROBE_SAMPLING_OBSERVER_WORKSPACE",
                "/run/stove0-ffprobe-sampling-observer",
            )
        ),
        source_revision=os.getenv("STOVE0_FFPROBE_SAMPLING_OBSERVER_SOURCE_REVISION", "unknown"),
        image_digest=_image_digest(),
    )
    token = _secret()
    with contextlib.suppress(KeyError):
        os.environ.pop("STOVE0_FFPROBE_SAMPLING_OBSERVER_TOKEN")
    uvicorn.run(create_app(token=token, observer=observer), host=args.host, port=args.port)
    return 0


def _image_digest() -> str:
    value = os.getenv("STOVE0_FFPROBE_SAMPLING_OBSERVER_IMAGE_DIGEST", "").strip()
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(
            "STOVE0_FFPROBE_SAMPLING_OBSERVER_IMAGE_DIGEST must be a lowercase SHA-256"
        )
    return value


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["SERVICE", "create_app", "main"]

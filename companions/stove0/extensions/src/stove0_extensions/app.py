"""Authenticated HTTP deployment for maintained stove0 extensions."""

from __future__ import annotations

import argparse
import contextlib
import importlib.metadata
import os
import secrets
import subprocess
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, cast

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from http_api_contracts import error_payload
from stove0_observer_support import ObserverHttpBinding, ObserverHttpResponse
from stove0_target_support import TargetHttpBinding, TargetHttpResponse

from stove0_extensions.observer import MediaSamplingObserver
from stove0_extensions.target_service import (
    LocalMediaTargetService,
    NvencMediaTargetService,
    PersistentTargetService,
)

ExtensionMode = Literal["observer", "local-target", "nvenc-target"]


def create_app(
    *,
    mode: ExtensionMode,
    token: str,
    observer: MediaSamplingObserver | None = None,
    target: PersistentTargetService | None = None,
) -> FastAPI:
    credential = token.strip()
    if not credential:
        raise ValueError("maintained extension token must be nonempty")
    ffmpeg = os.getenv("STOVE0_FFMPEG_BIN", "ffmpeg").strip()
    ffprobe = os.getenv("STOVE0_FFPROBE_BIN", "ffprobe").strip()
    if not ffmpeg or not ffprobe:
        raise ValueError("configured ffmpeg and ffprobe commands must be nonempty")
    if mode == "observer":
        implementation = observer or MediaSamplingObserver(ffprobe=ffprobe)
        observer_binding: ObserverHttpBinding | None = ObserverHttpBinding(implementation)
        target_binding: TargetHttpBinding | None = None
        target_service = None
    else:
        target_service = target or _target_from_environment(mode)
        observer_binding = None
        target_binding = TargetHttpBinding(target_service)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if target_service is not None:
                target_service.close()

    app = FastAPI(
        title=f"stove0 maintained {mode}",
        version="1",
        lifespan=lifespan,
        openapi_url="/v1/openapi.json",
    )

    @app.get("/health/live", tags=["health"])
    def live() -> dict[str, str]:
        return {"service": f"stove0-{mode}", "status": "ok"}

    @app.get("/health/ready", tags=["health"])
    def ready() -> Response:
        required = ffprobe if mode == "observer" else ffmpeg
        command = [required, "-version"]
        result = subprocess.run(command, check=False, capture_output=True, timeout=15)
        if result.returncode:
            return _error(503, "service_unavailable", f"{required} is not ready")
        if mode == "nvenc-target":
            encoders = subprocess.run(
                [ffmpeg, "-hide_banner", "-encoders"],
                check=False,
                capture_output=True,
                timeout=30,
            )
            if encoders.returncode or b"av1_nvenc" not in encoders.stdout:
                return _error(503, "service_unavailable", "AV1 NVENC is not available")
        return Response(
            content=b'{"service":"stove0-maintained-extension","status":"ok"}',
            media_type="application/json",
        )

    async def dispatch(request: Request) -> Response:
        authorization = request.headers.get("authorization", "")
        scheme, _, supplied = authorization.partition(" ")
        if (
            scheme.casefold() != "bearer"
            or not supplied
            or not secrets.compare_digest(supplied, credential)
        ):
            return _error(401, "unauthorized", "Bearer credential is not authorized")
        body = await request.body()
        result: ObserverHttpResponse | TargetHttpResponse
        if observer_binding is not None:
            result = await run_in_threadpool(
                observer_binding.handle,
                request.method,
                request.url.path,
                body,
            )
        elif target_binding is not None:
            result = await run_in_threadpool(
                target_binding.handle,
                request.method,
                request.url.path,
                body,
            )
        else:  # pragma: no cover - construction makes this unreachable
            raise AssertionError("extension binding is absent")
        return Response(
            content=result.body,
            status_code=result.status,
            headers=dict(result.headers),
        )

    if mode == "observer":
        app.add_api_route(
            "/v1/observer",
            dispatch,
            methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            tags=["observer"],
        )
        app.add_api_route(
            "/v1/observe",
            dispatch,
            methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            tags=["observer"],
        )
    else:
        for path in (
            "/v1/target",
            "/v1/preflight",
            "/v1/jobs/{job_id}",
            "/v1/jobs/{job_id}/cancel",
        ):
            app.add_api_route(
                path,
                dispatch,
                methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                tags=["target"],
            )
    return app


def _target_from_environment(mode: ExtensionMode) -> PersistentTargetService:
    state_root = Path(os.getenv("STOVE0_TARGET_STATE_ROOT", "/var/lib/stove0-target"))
    workspace_root = Path(os.getenv("STOVE0_EXTENSION_WORKSPACE", "/run/stove0-workspaces"))
    ffmpeg = os.getenv("STOVE0_FFMPEG_BIN", "ffmpeg").strip()
    ffprobe = os.getenv("STOVE0_FFPROBE_BIN", "ffprobe").strip()
    if mode == "local-target":
        return LocalMediaTargetService(
            state_root=state_root,
            workspace_root=workspace_root,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
        )
    if mode == "nvenc-target":
        return NvencMediaTargetService(
            state_root=state_root,
            workspace_root=workspace_root,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
        )
    raise ValueError("observer mode does not construct a target")


def _error(status: int, code: str, message: str) -> Response:
    import json

    return Response(
        content=json.dumps(error_payload(code=code, message=message), separators=(",", ":")),
        status_code=status,
        media_type="application/json",
    )


def _secret() -> str:
    direct = os.getenv("STOVE0_EXTENSION_TOKEN")
    path = os.getenv("STOVE0_EXTENSION_TOKEN_FILE")
    if bool(direct) == bool(path):
        raise ValueError("set exactly one of STOVE0_EXTENSION_TOKEN or STOVE0_EXTENSION_TOKEN_FILE")
    value = direct if direct is not None else Path(cast(str, path)).read_text(encoding="utf-8")
    token = value.strip()
    if not token:
        raise ValueError("maintained extension token must be nonempty")
    return token


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stove0-maintained-extension")
    parser.add_argument(
        "--version",
        action="version",
        version=importlib.metadata.version("stove0-maintained-extensions"),
    )
    parser.add_argument("mode", choices=("observer", "local-target", "nvenc-target"))
    parser.add_argument("--host", default=os.getenv("STOVE0_EXTENSION_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("STOVE0_EXTENSION_PORT", "8080")))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    token = _secret()
    with contextlib.suppress(KeyError):
        os.environ.pop("STOVE0_EXTENSION_TOKEN")
    uvicorn.run(
        create_app(mode=cast(ExtensionMode, args.mode), token=token),
        host=str(args.host),
        port=int(args.port),
    )
    return 0


__all__ = ["ExtensionMode", "create_app", "main"]

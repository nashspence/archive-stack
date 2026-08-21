"""Authenticated single-role ASGI storage-adapter service."""

from __future__ import annotations

import secrets
from collections.abc import Callable

from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from riverhog_storage_adapter_protocol import (
    StorageAdapterError,
    StorageAdapterErrorBody,
    StorageAdapterErrorCode,
    StorageAdapterPort,
)
from riverhog_storage_adapter_support.http_binding import (
    STORAGE_ADAPTER_HTTP_PATHS,
    StorageAdapterHttpBinding,
)


def create_storage_adapter_app(
    *,
    service: str,
    token: str,
    adapter: StorageAdapterPort,
    readiness: Callable[[], None] | None = None,
) -> FastAPI:
    """Return one authenticated application over a direct capability port."""

    credential = token.strip()
    if not credential:
        raise ValueError("storage adapter token must be nonempty")
    if not service.strip():
        raise ValueError("storage adapter service identity must be nonempty")
    binding = StorageAdapterHttpBinding(adapter)
    app = FastAPI(title=service, version="1", openapi_url="/v1/openapi.json")

    @app.get("/health/live", tags=["health"])
    def live() -> dict[str, str]:
        return {"service": service, "status": "ok"}

    @app.get("/health/ready", tags=["health"])
    def ready() -> Response:
        try:
            if readiness is not None:
                readiness()
            adapter.descriptor()
        except Exception:
            return _error(503, "provider_unavailable", "storage adapter is not ready")
        return JSONResponse({"service": service, "status": "ok"})

    async def dispatch(request: Request) -> Response:
        scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
        if scheme.casefold() != "bearer" or not secrets.compare_digest(supplied, credential):
            return _error(401, "unauthorized", "Bearer credential is not authorized")
        result = await run_in_threadpool(
            binding.handle,
            request.method,
            request.url.path,
            await request.body(),
        )
        headers = dict(result.headers)
        if isinstance(result.body, bytes):
            return Response(content=result.body, status_code=result.status, headers=headers)
        return StreamingResponse(
            result.body,
            status_code=result.status,
            headers=headers,
        )

    for path in sorted(STORAGE_ADAPTER_HTTP_PATHS):
        app.add_api_route(
            path,
            dispatch,
            methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            tags=["storage-adapter"],
        )
    return app


def _error(status: int, code: StorageAdapterErrorCode, message: str) -> Response:
    payload = StorageAdapterError(error=StorageAdapterErrorBody(code=code, message=message))
    return Response(
        content=payload.model_dump_json().encode(),
        status_code=status,
        media_type="application/json",
    )


__all__ = ["create_storage_adapter_app"]

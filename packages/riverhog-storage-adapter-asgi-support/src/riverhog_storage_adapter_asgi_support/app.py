"""Authenticated single-role ASGI storage-adapter service."""

from __future__ import annotations

import asyncio
import queue
import secrets
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from fastapi import Depends, FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPBearer
from http_api_contracts import FRAMED_BODY_MEDIA_TYPE, HealthResponse, operation_openapi
from riverhog_storage_adapter_protocol import (
    StorageAdapterError,
    StorageAdapterErrorBody,
    StorageAdapterErrorCode,
    StorageAdapterPort,
)
from riverhog_storage_adapter_support.http_binding import (
    FRAMED_STORAGE_ADAPTER_HTTP_PATHS,
    STORAGE_ADAPTER_HTTP_OPERATIONS,
    StorageAdapterHttpBinding,
)

_PUBLIC_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_STREAM_CHUNK_BYTES = 1024 * 1024
_STREAM_QUEUE_CHUNKS = 4
_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class _BodyFailure:
    error: BaseException


_BODY_END = object()


class _RequestBodyBridge(Iterator[bytes]):
    """Bound one asynchronous request stream to one synchronous adapter call."""

    def __init__(self) -> None:
        self._items: queue.Queue[bytes | _BodyFailure | object] = queue.Queue(
            maxsize=_STREAM_QUEUE_CHUNKS
        )
        self._closed = threading.Event()

    def __iter__(self) -> _RequestBodyBridge:
        return self

    def __next__(self) -> bytes:
        item = self._items.get()
        if item is _BODY_END:
            raise StopIteration
        if isinstance(item, _BodyFailure):
            raise RuntimeError("request body stream failed") from item.error
        if not isinstance(item, bytes):  # pragma: no cover - private queue invariant
            raise RuntimeError("request body bridge received an invalid item")
        return item

    async def feed(self, request: Request) -> None:
        try:
            async for chunk in request.stream():
                for offset in range(0, len(chunk), _STREAM_CHUNK_BYTES):
                    if not await asyncio.to_thread(
                        self._put,
                        bytes(chunk[offset : offset + _STREAM_CHUNK_BYTES]),
                    ):
                        return
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            await asyncio.to_thread(self._put, _BodyFailure(exc))
        else:
            await asyncio.to_thread(self._put, _BODY_END)

    def close(self) -> None:
        self._closed.set()

    def _put(self, item: bytes | _BodyFailure | object) -> bool:
        while not self._closed.is_set():
            try:
                self._items.put(item, timeout=0.1)
            except queue.Full:
                continue
            return True
        return False


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

    @app.get("/health/live", response_model=HealthResponse, tags=["health"])
    def live() -> dict[str, str]:
        return {"service": service, "status": "ok"}

    @app.get(
        "/health/ready",
        response_model=HealthResponse,
        responses={503: {"model": StorageAdapterError}},
        tags=["health"],
    )
    def ready() -> Response:
        try:
            if readiness is not None:
                readiness()
            binding.adapter.descriptor()
        except Exception:
            return _error(503, "provider_unavailable", "storage adapter is not ready")
        return JSONResponse({"service": service, "status": "ok"})

    async def dispatch(request: Request) -> Response:
        scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
        if scheme.casefold() != "bearer" or not secrets.compare_digest(supplied, credential):
            return _error(401, "unauthorized", "Bearer credential is not authorized")
        if request.method.upper() == "POST" and request.url.path in (
            FRAMED_STORAGE_ADAPTER_HTTP_PATHS
        ):
            media_type = request.headers.get("content-type", "").partition(";")[0].strip()
            if media_type.casefold() != FRAMED_BODY_MEDIA_TYPE.casefold():
                return _error(400, "invalid_request", "framed request media type is invalid")
            raw_length = request.headers.get("content-length")
            if raw_length is None:
                return _error(
                    411,
                    "length_required",
                    "framed adapter requests require Content-Length",
                )
            try:
                content_length = int(raw_length)
            except ValueError:
                return _error(400, "invalid_request", "request Content-Length is invalid")
            if content_length < 0:
                return _error(400, "invalid_request", "request Content-Length is invalid")
            bridge = _RequestBodyBridge()
            producer = asyncio.create_task(bridge.feed(request))
            try:
                result = await run_in_threadpool(
                    binding.handle_framed,
                    request.method,
                    request.url.path,
                    bridge,
                    content_length=content_length,
                )
            finally:
                bridge.close()
                if not producer.done():
                    producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)
        else:
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

    methods_by_path: dict[str, set[str]] = {}
    for operation in STORAGE_ADAPTER_HTTP_OPERATIONS:
        methods_by_path.setdefault(operation.path, set()).add(operation.method)
        app.add_api_route(
            operation.path,
            dispatch,
            methods=[operation.method],
            dependencies=[Depends(_bearer)],
            response_class=Response,
            tags=["storage-adapter"],
            **operation_openapi(operation, error_type=StorageAdapterError),
        )
    for path, supported in methods_by_path.items():
        app.add_api_route(
            path,
            dispatch,
            methods=sorted(_PUBLIC_METHODS - supported),
            include_in_schema=False,
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

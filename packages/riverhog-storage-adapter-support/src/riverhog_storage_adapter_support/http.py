"""Authenticated streaming HTTP binding for storage adapters."""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Header, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from riverhog_storage_adapter_protocol import (
    AbortIncompleteUploadsRequest,
    CompleteUploadRequest,
    ObjectDeleteRequest,
    ObjectLocator,
    PrefixDeleteRequest,
    ReadRequest,
    StorageAdapterError,
    UploadDeclaration,
)

from riverhog_storage_adapter_support.service import (
    StorageAdapterService,
    StorageAdapterServiceError,
)

_RANGE_RE = re.compile(r"bytes=([0-9]+)-([0-9]+)$")


def create_storage_adapter_app(
    *,
    service_name: str,
    token: str,
    service: StorageAdapterService,
) -> FastAPI:
    """Create the conventional v1 adapter service without provider knowledge."""

    credential = token.strip()
    if not credential:
        raise ValueError("storage-adapter bearer token must not be empty")

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    app = FastAPI(
        title=service_name,
        version="1",
        openapi_url="/v1/openapi.json",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def authorize(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if not request.url.path.startswith("/v1/"):
            return await call_next(request)
        scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
        if scheme.casefold() != "bearer" or not secrets.compare_digest(supplied, credential):
            return JSONResponse(
                status_code=401,
                content=StorageAdapterError(
                    code="unauthorized",
                    message="Bearer credential is not authorized",
                ).model_dump(mode="json"),
            )
        return await call_next(request)

    @app.exception_handler(StorageAdapterServiceError)
    async def service_error(
        _request: Request,
        exc: StorageAdapterServiceError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content=StorageAdapterError(code=exc.code, message=exc.message).model_dump(mode="json"),
        )

    @app.get("/health/live", tags=["health"])
    def live() -> dict[str, str]:
        return {"service": service_name, "status": "ok"}

    @app.get("/health/ready", tags=["health"])
    def ready() -> Response:
        try:
            service.ready()
        except Exception:
            return JSONResponse(
                status_code=503,
                content={"service": service_name, "status": "unavailable"},
            )
        return JSONResponse(content={"service": service_name, "status": "ok"})

    @app.get("/v1/adapter", tags=["storage-adapter"])
    def descriptor() -> object:
        return service.descriptor().model_dump(mode="json")

    @app.put("/v1/uploads/{transfer_id}", tags=["storage-adapter"])
    def put_upload(
        transfer_id: str,
        declaration: UploadDeclaration,
    ) -> object:
        if declaration.transfer_id != transfer_id:
            raise StorageAdapterServiceError(
                409,
                "upload_conflict",
                "path transfer ID differs from the declaration",
            )
        return service.put_upload(declaration).model_dump(mode="json", exclude_none=True)

    @app.get("/v1/uploads/{transfer_id}", tags=["storage-adapter"])
    def get_upload(transfer_id: str) -> object:
        return service.get_upload(transfer_id).model_dump(mode="json", exclude_none=True)

    @app.put("/v1/uploads/{transfer_id}/parts/{part_number}", tags=["storage-adapter"])
    async def put_part(
        transfer_id: str,
        part_number: int,
        request: Request,
        content_length: Annotated[int, Header(alias="Content-Length", ge=1)],
        stored_sha256: Annotated[
            str,
            Header(alias="X-Riverhog-Stored-Sha256", pattern=r"^[0-9a-f]{64}$"),
        ],
    ) -> object:
        content = await request.body()
        if len(content) != content_length:
            raise StorageAdapterServiceError(
                422,
                "integrity_failure",
                "upload part length does not match Content-Length",
            )
        return service.put_part(
            transfer_id=transfer_id,
            number=part_number,
            content=content,
            stored_sha256=stored_sha256,
        ).model_dump(mode="json")

    @app.post("/v1/uploads/{transfer_id}/complete", tags=["storage-adapter"])
    def complete_upload(
        transfer_id: str,
        completion: CompleteUploadRequest,
    ) -> object:
        return service.complete_upload(
            transfer_id=transfer_id,
            completion=completion,
        ).model_dump(mode="json")

    @app.delete("/v1/uploads/{transfer_id}", tags=["storage-adapter"])
    def delete_upload(transfer_id: str) -> object:
        return service.delete_upload(transfer_id).model_dump(mode="json", exclude_none=True)

    @app.get("/v1/objects/metadata", tags=["storage-adapter"])
    def object_metadata(
        path: Annotated[str, Query(min_length=1, max_length=4096)],
        revision: Annotated[str, Query(min_length=1, max_length=1000)],
    ) -> object:
        locator = ObjectLocator(object_path=path, revision=revision)
        return service.object_metadata(locator).model_dump(mode="json")

    @app.get("/v1/objects/content", tags=["storage-adapter"])
    def object_content(
        request: Request,
        path: Annotated[str, Query(min_length=1, max_length=4096)],
        revision: Annotated[str, Query(min_length=1, max_length=1000)],
    ) -> StreamingResponse:
        locator = ObjectLocator(object_path=path, revision=revision)
        metadata = service.object_metadata(locator)
        offset: int | None = None
        size: int | None = None
        status = 200
        headers = {
            "Accept-Ranges": "bytes",
            "X-Riverhog-Object-Revision": metadata.revision,
            "X-Riverhog-Stored-Sha256": metadata.stored_sha256,
        }
        range_header = request.headers.get("range")
        if range_header is not None:
            match = _RANGE_RE.fullmatch(range_header)
            if match is None:
                raise StorageAdapterServiceError(
                    416,
                    "invalid_range",
                    "only one exact byte range is supported",
                )
            offset = int(match.group(1))
            end = int(match.group(2))
            if end < offset or end >= metadata.stored_bytes:
                raise StorageAdapterServiceError(
                    416,
                    "invalid_range",
                    "requested byte range is outside the object",
                )
            size = end - offset + 1
            status = 206
            headers["Content-Range"] = f"bytes {offset}-{end}/{metadata.stored_bytes}"
            headers["Content-Length"] = str(size)
        else:
            headers["Content-Length"] = str(metadata.stored_bytes)
        return StreamingResponse(
            service.iter_object_content(locator, offset=offset, size=size),
            status_code=status,
            media_type=metadata.content_type,
            headers=headers,
        )

    @app.delete("/v1/objects", tags=["storage-adapter"], status_code=204)
    def delete_object(
        deletion: ObjectDeleteRequest,
    ) -> Response:
        service.delete_object(deletion.object)
        return Response(status_code=204)

    @app.post("/v1/object-prefixes/delete", tags=["storage-adapter"])
    def delete_prefix(
        deletion: PrefixDeleteRequest,
    ) -> object:
        return service.delete_prefix(deletion.object_prefix).model_dump(mode="json")

    @app.post("/v1/reads/prepare", tags=["storage-adapter"])
    def prepare_read(request: ReadRequest) -> object:
        return service.prepare_read(request).model_dump(mode="json", exclude_none=True)

    @app.post("/v1/reads/status", tags=["storage-adapter"])
    def read_status(request: ReadRequest) -> object:
        return service.read_status(request).model_dump(mode="json", exclude_none=True)

    @app.post("/v1/reads/cleanup", tags=["storage-adapter"], status_code=204)
    def cleanup_read(request: ReadRequest) -> Response:
        service.cleanup_read(request)
        return Response(status_code=204)

    @app.post(
        "/v1/maintenance/abort-incomplete-uploads",
        tags=["storage-adapter"],
    )
    def abort_incomplete_uploads(
        request: AbortIncompleteUploadsRequest,
    ) -> object:
        return service.abort_incomplete_uploads(
            initiated_before=request.initiated_before
        ).model_dump(mode="json")

    return app


def storage_adapter_openapi_json(app: FastAPI) -> bytes:
    return json.dumps(app.openapi(), sort_keys=True, separators=(",", ":")).encode("utf-8")


__all__ = ["create_storage_adapter_app", "storage_adapter_openapi_json"]

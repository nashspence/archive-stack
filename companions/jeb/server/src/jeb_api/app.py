from __future__ import annotations

import importlib.metadata
import secrets
import socket
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from typing import Annotated, Any

import uvicorn
from fastapi import APIRouter, Body, Depends, FastAPI, Header, Query, Request, Response, Security
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from http_api_contracts import (
    PUBLIC_ERROR_RESPONSES,
    apply_openapi_error_contract,
    error_payload,
    status_for_error_code,
)
from jeb_core.domain.models import UnrecoverableJebError
from jeb_core.domain.sources import SourceNotFoundError, SourceRegistryError
from jeb_core.ingress import (
    JebIngressAuthenticationError,
    JebIngressError,
    authenticate_tus_source,
)
from jeb_protocol import ATTEMPT_LIST_SORT_FIELDS
from riverhog_provenance import ProvenanceValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from jeb_api.composition import JebServices, config_from_env, create_services
from jeb_api.schemas import (
    ArchiveNowIn,
    AttemptOut,
    AttemptPageOut,
    ConfigCheckOut,
    CredentialRotateIn,
    ErrorResponse,
    EventPage,
    HealthResponse,
    IngressPublicationOut,
    OperationOut,
    OperationPageOut,
    OperationStartedOut,
    SourceCreatedOut,
    SourceCreateIn,
    SourceOut,
    SourcePageOut,
    SourceRemovalIn,
    SourceRemovalPlanIn,
    SourceUpdateIn,
    StatusOut,
)

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: response for status, response in PUBLIC_ERROR_RESPONSES.items()
}
JEB_BEARER = HTTPBearer(
    auto_error=False,
    scheme_name="JebBearer",
    description="Jeb management API bearer token.",
)


class SortOrder(StrEnum):
    ASC = "asc"
    DESC = "desc"


class ResolutionFilter(StrEnum):
    UNRESOLVED = "unresolved"
    RESOLVED = "resolved"
    ALL = "all"


@dataclass(frozen=True)
class JebServiceState:
    services: JebServices

    @property
    def api_token(self) -> str:
        return self.services.config.management_api_token


@dataclass(frozen=True)
class JebHttpError(Exception):
    status: HTTPStatus
    code: str
    message: str
    headers: Mapping[str, str] | None = None


def authorize_management_api(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(JEB_BEARER)],
) -> None:
    state: JebServiceState = request.app.state.jeb
    if (
        credentials is not None
        and credentials.scheme.casefold() == "bearer"
        and secrets.compare_digest(credentials.credentials, state.api_token)
    ):
        return
    raise JebHttpError(
        HTTPStatus.UNAUTHORIZED,
        "unauthorized",
        "valid Jeb bearer credentials are required",
        {"WWW-Authenticate": "Bearer"},
    )


class JebServiceServer:
    def __init__(
        self,
        server: uvicorn.Server,
        thread: threading.Thread,
        listener: socket.socket,
    ) -> None:
        self._server = server
        self._thread = thread
        self._listener = listener

    @property
    def server_address(self) -> tuple[str, int]:
        address = self._listener.getsockname()
        return str(address[0]), int(address[1])

    def shutdown(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            raise RuntimeError("Jeb API server did not stop")

    def server_close(self) -> None:
        try:
            self._listener.close()
        except OSError:
            pass


def _error_response(
    status: HTTPStatus | int,
    *,
    code: str,
    message: str,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_for_error_code(code, fallback=int(status)),
        content=error_payload(code=code, message=message),
        headers=dict(headers or {}),
    )


def _validation_message(exc: RequestValidationError) -> str:
    first = exc.errors()[0]
    location = ".".join(str(item) for item in first["loc"] if item not in {"body", "query"})
    message = str(first["msg"])
    if message == "Input should be a valid boolean":
        message = "must be true or false"
    return f"{location} {message}" if location else message


def _tus_rejection(status: HTTPStatus, message: str) -> dict[str, Any]:
    return {
        "RejectUpload": True,
        "HTTPResponse": {
            "StatusCode": int(status),
            "Body": message,
            "Header": {"Content-Type": "text/plain"},
        },
    }


def create_app(state: JebServiceState | None = None) -> FastAPI:
    resolved_state = state or JebServiceState(
        services=create_services(config_from_env()),
    )
    services = resolved_state.services
    app = FastAPI(
        title="Jeb API",
        version=importlib.metadata.version("jeb-server"),
        description="Source enrollment, watched-drop ingestion, and target delivery management.",
    )
    app.state.jeb = resolved_state

    @app.exception_handler(JebHttpError)
    async def handle_jeb_http_error(_request: object, exc: JebHttpError) -> JSONResponse:
        return _error_response(
            exc.status,
            code=exc.code,
            message=exc.message,
            headers=exc.headers,
        )

    @app.exception_handler(SourceNotFoundError)
    async def handle_source_not_found(
        _request: object,
        exc: SourceNotFoundError,
    ) -> JSONResponse:
        return _error_response(HTTPStatus.NOT_FOUND, code="not_found", message=str(exc))

    @app.exception_handler(SourceRegistryError)
    async def handle_source_registry_error(
        _request: object,
        exc: SourceRegistryError,
    ) -> JSONResponse:
        return _error_response(HTTPStatus.BAD_REQUEST, code="bad_request", message=str(exc))

    @app.exception_handler(UnrecoverableJebError)
    async def handle_invalid_state(
        _request: object,
        exc: UnrecoverableJebError,
    ) -> JSONResponse:
        return _error_response(HTTPStatus.CONFLICT, code="invalid_state", message=str(exc))

    @app.exception_handler(JebIngressError)
    async def handle_ingress_error(_request: object, exc: JebIngressError) -> JSONResponse:
        return _error_response(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            code="ingress_failed",
            message=str(exc),
        )

    @app.exception_handler(JebIngressAuthenticationError)
    async def handle_ingress_authentication_error(
        _request: object,
        _exc: JebIngressAuthenticationError,
    ) -> JSONResponse:
        return _error_response(
            HTTPStatus.UNAUTHORIZED,
            code="unauthorized",
            message="valid Jeb ingress credentials are required",
            headers={"WWW-Authenticate": 'Basic realm="Jeb ingress"'},
        )

    @app.exception_handler(ValueError)
    async def handle_value_error(_request: object, exc: ValueError) -> JSONResponse:
        return _error_response(HTTPStatus.BAD_REQUEST, code="bad_request", message=str(exc))

    @app.exception_handler(ProvenanceValidationError)
    async def handle_provenance_error(
        _request: object,
        exc: ProvenanceValidationError,
    ) -> JSONResponse:
        return _error_response(
            HTTPStatus.BAD_REQUEST,
            code="invalid_provenance",
            message=str(exc),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: object,
        exc: RequestValidationError,
    ) -> JSONResponse:
        return _error_response(
            HTTPStatus.BAD_REQUEST,
            code="bad_request",
            message=_validation_message(exc),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(_request: object, exc: StarletteHTTPException) -> JSONResponse:
        status = HTTPStatus(exc.status_code)
        code = {
            HTTPStatus.UNAUTHORIZED: "unauthorized",
            HTTPStatus.NOT_FOUND: "not_found",
            HTTPStatus.METHOD_NOT_ALLOWED: "method_not_allowed",
        }.get(status, "bad_request")
        return _error_response(
            status,
            code=code,
            message=str(exc.detail),
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_request: object, _exc: Exception) -> JSONResponse:
        return _error_response(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            code="internal_error",
            message="internal server error",
        )

    @app.get(
        "/health/live",
        response_model=HealthResponse,
        operation_id="health_live",
        tags=["health"],
    )
    def health_live() -> dict[str, Any]:
        return {"service": "jeb", "status": "ok"}

    @app.get(
        "/health/ready",
        response_model=HealthResponse,
        responses={503: {"model": ErrorResponse}},
        operation_id="health_ready",
        tags=["health"],
    )
    def health_ready() -> dict[str, Any]:
        try:
            services.runtime.initialize()
        except Exception as exc:
            raise JebHttpError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "service_unavailable",
                "Jeb runtime dependencies are not ready",
            ) from exc
        return {"service": "jeb", "status": "ok"}

    internal = APIRouter(prefix="/internal", tags=["internal"])

    @internal.get(
        "/ingress/tus/auth",
        status_code=HTTPStatus.NO_CONTENT,
        operation_id="authenticate_tus_ingress",
    )
    def authenticate_tus_ingress(
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        services.runtime.initialize()
        try:
            authenticate_tus_source(services.source_registry, authorization)
        except JebIngressAuthenticationError:
            return Response(
                status_code=HTTPStatus.UNAUTHORIZED,
                headers={"WWW-Authenticate": 'Basic realm="Jeb ingress"'},
            )
        return Response(status_code=HTTPStatus.NO_CONTENT)

    @internal.put(
        "/ingress/tus/provenance/{upload_id}/journals/{journal_id}",
        operation_id="put_tus_ingress_provenance_journal",
    )
    def put_tus_ingress_provenance_journal(
        upload_id: str,
        journal_id: str,
        content: Annotated[bytes, Body(media_type="application/json-seq")],
        authorization: Annotated[str | None, Header()] = None,
        x_riverhog_provenance_sha256: Annotated[str, Header()] = "",
    ) -> dict[str, object]:
        services.runtime.initialize()
        return services.ingress.put_journal(
            authorization=authorization,
            upload_id=upload_id,
            journal_id=journal_id,
            content=content,
            expected_sha256=x_riverhog_provenance_sha256,
        )

    @internal.put(
        "/ingress/tus/provenance/{upload_id}/binding",
        operation_id="put_tus_ingress_provenance_binding",
    )
    def put_tus_ingress_provenance_binding(
        upload_id: str,
        payload: Annotated[dict[str, object], Body()],
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        services.runtime.initialize()
        return services.ingress.put_binding(
            authorization=authorization,
            upload_id=upload_id,
            payload=payload,
        )

    @internal.get(
        "/ingress/tus/publications/{upload_id}",
        response_model=IngressPublicationOut,
        response_model_exclude_none=True,
        operation_id="get_tus_ingress_publication",
    )
    def get_tus_ingress_publication(
        upload_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        services.runtime.initialize()
        return services.ingress.receipt(upload_id, authorization=authorization)

    @internal.post(
        "/ingress/tus/hooks",
        operation_id="handle_tus_hook",
    )
    def handle_tus_hook(
        payload: Annotated[dict[str, Any], Body()],
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        hook_type = str(payload.get("Type") or "")
        event = payload.get("Event")
        upload = event.get("Upload") if isinstance(event, Mapping) else None
        if not isinstance(upload, Mapping):
            raise JebIngressError("Jeb TUS hook is missing its upload record")
        if hook_type == "pre-create":
            metadata = upload.get("MetaData")
            if not isinstance(metadata, Mapping):
                metadata = {}
            try:
                prepared = services.ingress.prepare(
                    authorization=authorization,
                    metadata=metadata,
                    size=upload.get("Size"),
                )
            except JebIngressAuthenticationError:
                return _tus_rejection(
                    HTTPStatus.UNAUTHORIZED,
                    "invalid Jeb ingress credentials",
                )
            except JebIngressError:
                return _tus_rejection(
                    HTTPStatus.BAD_REQUEST,
                    "invalid Jeb TUS upload",
                )
            return {
                "ChangeFileInfo": {
                    "ID": prepared.upload_id,
                    "MetaData": prepared.hook_metadata(),
                }
            }
        if hook_type == "post-finish":
            services.ingress.publish(upload)
        return {}

    management = APIRouter(
        prefix="/v1",
        dependencies=[Depends(authorize_management_api)],
        responses=ERROR_RESPONSES,
    )

    @management.get(
        "/events",
        response_model=EventPage,
        operation_id="list_lifecycle_events",
        tags=["events"],
    )
    def list_lifecycle_events(
        after: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> EventPage:
        services.runtime.initialize()
        return services.event_log.page(after=after, limit=limit)

    @management.get(
        "/config/check",
        response_model=ConfigCheckOut,
        operation_id="check_config",
        tags=["service"],
    )
    def check_config() -> dict[str, Any]:
        services.runtime.initialize()
        return {
            "status": "ok",
            "source_count": services.source_registry.count(),
        }

    @management.get(
        "/operations",
        response_model=OperationPageOut,
        response_model_exclude_none=True,
        operation_id="list_operations",
        tags=["operations"],
    )
    def list_operations(
        page: Annotated[int, Query(ge=1)] = 1,
        per_page: Annotated[int, Query(ge=1, le=100)] = 25,
        sort: str = "started_at",
        order: SortOrder = SortOrder.DESC,
        query: Annotated[str | None, Query(alias="q")] = None,
        state_filter: Annotated[str | None, Query(alias="state")] = None,
        all_items: Annotated[bool, Query(alias="all")] = False,
    ) -> dict[str, Any]:
        return services.operations.list_page(
            page=page,
            per_page=per_page,
            sort=sort,
            order=order.value,
            query=query,
            state=state_filter,
            all_items=all_items,
        )

    @management.get(
        "/operations/{operation_id}",
        response_model=OperationOut,
        response_model_exclude_none=True,
        operation_id="get_operation",
        tags=["operations"],
    )
    def get_operation(operation_id: str) -> dict[str, Any]:
        try:
            return services.operations.get(operation_id)
        except KeyError as exc:
            raise JebHttpError(
                HTTPStatus.NOT_FOUND,
                "not_found",
                f"Jeb operation not found: {operation_id}",
            ) from exc

    @management.get(
        "/sources",
        response_model=SourcePageOut,
        operation_id="list_sources",
        tags=["sources"],
    )
    def list_sources(
        page: Annotated[int, Query(ge=1)] = 1,
        per_page: Annotated[int, Query(ge=1, le=100)] = 25,
        sort: str = "id",
        order: SortOrder = SortOrder.ASC,
        query: Annotated[str | None, Query(alias="q")] = None,
        enabled: bool | None = None,
        adapter: str | None = None,
        target: str | None = None,
        all_items: Annotated[bool, Query(alias="all")] = False,
    ) -> dict[str, Any]:
        services.runtime.initialize()
        return services.source_registry.list_page(
            page=page,
            per_page=per_page,
            sort=sort,
            order=order.value,
            query=query,
            enabled=enabled,
            adapter=adapter,
            target=target,
            all_items=all_items,
        )

    @management.get(
        "/sources/{source_id}",
        response_model=SourceOut,
        operation_id="get_source",
        tags=["sources"],
    )
    def get_source(source_id: str) -> dict[str, Any]:
        services.runtime.initialize()
        return services.source_registry.get(source_id).summary()

    @management.get(
        "/status",
        response_model=StatusOut,
        operation_id="get_status",
        tags=["service"],
    )
    def get_status(include_backlog: bool = True) -> dict[str, Any]:
        services.runtime.initialize()
        payload = services.runtime.status_summary(include_backlog=include_backlog)
        payload["active_operation"] = services.operations.active_summary()
        return payload

    @management.get(
        "/attempts",
        response_model=AttemptPageOut,
        operation_id="list_attempts",
        tags=["attempts"],
    )
    def list_attempts(
        page: Annotated[int, Query(ge=1)] = 1,
        per_page: Annotated[int, Query(ge=1, le=100)] = 25,
        sort: str = "updated_at",
        order: SortOrder = SortOrder.DESC,
        query: Annotated[str | None, Query(alias="q")] = None,
        resolution: ResolutionFilter = ResolutionFilter.UNRESOLVED,
        state_filter: Annotated[str | None, Query(alias="state")] = None,
        source: str | None = None,
        target: str | None = None,
        all_items: Annotated[bool, Query(alias="all")] = False,
    ) -> dict[str, Any]:
        if sort not in ATTEMPT_LIST_SORT_FIELDS:
            raise ValueError("sort must be one of: " + ", ".join(sorted(ATTEMPT_LIST_SORT_FIELDS)))
        services.runtime.initialize()
        return services.store.list_attempts(
            page=page,
            per_page=per_page,
            sort=sort,
            order=order.value,
            query=query,
            resolution=resolution.value,
            state=state_filter,
            source=source,
            target=target,
            all_items=all_items,
        )

    @management.get(
        "/attempts/{attempt_id}",
        response_model=AttemptOut,
        operation_id="get_attempt",
        tags=["attempts"],
    )
    def get_attempt(attempt_id: str) -> dict[str, Any]:
        services.runtime.initialize()
        try:
            return services.store.get_attempt(attempt_id)
        except KeyError as exc:
            raise JebHttpError(
                HTTPStatus.NOT_FOUND,
                "not_found",
                f"Jeb attempt not found: {attempt_id}",
            ) from exc

    @management.post(
        "/sources",
        status_code=HTTPStatus.CREATED,
        response_model=SourceCreatedOut,
        response_model_exclude_none=True,
        operation_id="create_source",
        tags=["sources"],
    )
    def create_source(payload: SourceCreateIn) -> dict[str, Any]:
        source, credential = services.sources.add_source(
            payload.id.strip(),
            adapters=payload.adapters,
            target_config=payload.target_config,
            credential=payload.credential,
            enabled=bool(payload.enabled),
            stable_seconds=payload.stable_seconds,
            include_extensions=payload.include_extensions,
            target=payload.target,
            threshold_bytes=payload.threshold_bytes,
            cleanup=payload.cleanup,
            cadence=payload.cadence,
            weekday=payload.weekday,
            hour=payload.hour,
            minute=payload.minute,
        )
        response: dict[str, Any] = {"source": source.summary()}
        if credential is not None:
            response["credential"] = credential
        return response

    @management.post(
        "/sources/{source_id}/enable",
        response_model=SourceOut,
        operation_id="enable_source",
        tags=["sources"],
    )
    def enable_source(source_id: str) -> dict[str, Any]:
        services.runtime.initialize()
        return services.source_registry.set_enabled(source_id, True).summary()

    @management.post(
        "/sources/{source_id}/disable",
        response_model=SourceOut,
        operation_id="disable_source",
        tags=["sources"],
    )
    def disable_source(source_id: str) -> dict[str, Any]:
        services.runtime.initialize()
        return services.source_registry.set_enabled(source_id, False).summary()

    @management.post(
        "/sources/{source_id}/credential",
        response_model=SourceCreatedOut,
        response_model_exclude_none=True,
        operation_id="rotate_source_credential",
        tags=["sources"],
    )
    def rotate_source_credential(
        source_id: str,
        payload: CredentialRotateIn,
    ) -> dict[str, Any]:
        services.runtime.initialize()
        source, credential = services.source_registry.rotate_credential(
            source_id,
            credential=payload.credential,
        )
        response: dict[str, Any] = {"source": source.summary()}
        if credential is not None:
            response["credential"] = credential
        return response

    @management.post(
        "/sources/{source_id}/removal-plan",
        operation_id="plan_source_removal",
        tags=["sources"],
    )
    def plan_source_removal(
        source_id: str,
        payload: SourceRemovalPlanIn,
    ) -> dict[str, Any]:
        if services.operations.active_summary() is not None:
            raise UnrecoverableJebError(
                "a Jeb operation is running; request the removal plan again later"
            )
        return services.sources.source_removal_plan(source_id, purge=bool(payload.purge))

    @management.post(
        "/once",
        status_code=HTTPStatus.ACCEPTED,
        response_model=OperationStartedOut,
        response_model_exclude_none=True,
        operation_id="run_once",
        tags=["operations"],
    )
    def run_once() -> dict[str, Any]:
        services.runtime.initialize()
        operation = services.operations.start(
            operation="once",
            run=services.runtime.run_once,
        )
        return {"status": "started", "operation": operation}

    @management.post(
        "/archive-now",
        operation_id="archive_source_now",
        tags=["attempts"],
        responses={
            **ERROR_RESPONSES,
            200: {"description": "Archive plan or no eligible files."},
            202: {"description": "Archive attempt started."},
        },
    )
    def archive_source_now(payload: ArchiveNowIn) -> JSONResponse:
        services.runtime.initialize()
        if payload.dry_run:
            return JSONResponse(
                status_code=HTTPStatus.OK,
                content=services.attempts.archive_plan(
                    source_id=payload.source.strip(),
                    process=bool(payload.process),
                ),
            )
        operation: dict[str, Any] | None = None
        if payload.process:
            attempt_id, operation = services.operations.prepare_and_start(
                operation="archive-now",
                source=payload.source.strip(),
                prepare=lambda: services.attempts.archive_now(
                    source_id=payload.source.strip(),
                    process=False,
                ),
                run=services.attempts.process_attempt,
            )
        else:
            attempt_id = services.attempts.archive_now(
                source_id=payload.source.strip(),
                process=False,
            )
        if attempt_id is None:
            return JSONResponse(
                status_code=HTTPStatus.OK,
                content={"status": "no_eligible_files", "source": payload.source.strip()},
            )
        attempt = services.store.load_attempt(attempt_id)
        return JSONResponse(
            status_code=HTTPStatus.ACCEPTED,
            content={
                "status": "started",
                "source": payload.source.strip(),
                "attempt_id": attempt_id,
                "batch_id": str(attempt["batch_id"]),
                "operation": operation,
            },
        )

    @management.patch(
        "/sources/{source_id}",
        response_model=SourceOut,
        operation_id="update_source",
        tags=["sources"],
    )
    def update_source(source_id: str, payload: SourceUpdateIn) -> dict[str, Any]:
        changes = payload.model_dump(exclude_unset=True)
        source = services.sources.update_source(source_id, changes)
        return source.summary()

    @management.delete(
        "/attempts/{attempt_id}",
        response_model=AttemptOut,
        operation_id="cancel_attempt",
        tags=["attempts"],
    )
    def cancel_attempt(attempt_id: str) -> dict[str, Any]:
        services.runtime.initialize()
        try:
            return services.attempts.cancel_attempt(attempt_id)
        except KeyError as exc:
            raise JebHttpError(
                HTTPStatus.NOT_FOUND,
                "not_found",
                f"Jeb attempt not found: {attempt_id}",
            ) from exc

    @management.delete(
        "/sources/{source_id}",
        operation_id="remove_source",
        tags=["sources"],
    )
    def remove_source(source_id: str, payload: SourceRemovalIn) -> dict[str, Any]:
        if services.operations.active_summary() is not None:
            raise UnrecoverableJebError("a Jeb operation is running; source removal cannot begin")
        return services.sources.remove_source(source_id, challenge=payload.challenge)

    app.include_router(internal)
    app.include_router(management)
    app.openapi_schema = apply_openapi_error_contract(app.openapi())
    return app


def start_jeb_service_server(
    host: str,
    port: int,
    state: JebServiceState,
) -> JebServiceServer:
    state.services.operations.recover_interrupted()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host, port))
    listener.listen(2048)
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(state),
            host=host,
            port=port,
            log_level="error",
            access_log=False,
            ws="none",
        )
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        name="jeb-api",
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started:
        if not thread.is_alive():
            listener.close()
            raise RuntimeError("Jeb API server failed to start")
        if time.monotonic() >= deadline:
            server.should_exit = True
            thread.join(timeout=5)
            listener.close()
            raise RuntimeError("Jeb API server did not become ready")
        time.sleep(0.01)
    return JebServiceServer(server, thread, listener)


__all__ = [
    "JebServiceServer",
    "JebServiceState",
    "create_app",
    "start_jeb_service_server",
]

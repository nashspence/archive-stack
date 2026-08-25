from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.metadata
import json
import logging
import sys
import threading
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import timedelta

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from http_api_contracts import (
    apply_openapi_error_contract,
    error_code_for_status,
    error_payload,
    status_for_error_code,
)
from riverhog_core.catalog_db import catalog_state_schema
from riverhog_core.runtime_config import load_runtime_config
from riverhog_protocol.errors import RiverhogError, ServiceUnavailable
from starlette.exceptions import HTTPException as StarletteHTTPException
from state_schema import StateSchemaError
from time_formats import utc_now

from riverhog_api.auth import apply_openapi_permission_contract
from riverhog_api.deps import ServiceContainer, default_container, get_container
from riverhog_api.error_contracts import RIVERHOG_OPERATION_ERROR_CODES
from riverhog_api.routers.apps import router as apps_router
from riverhog_api.routers.archive import router as archive_router
from riverhog_api.routers.collections import router as collections_router
from riverhog_api.routers.events import router as events_router
from riverhog_api.routers.provenance import router as provenance_router
from riverhog_api.routers.quotas import router as quotas_router
from riverhog_api.routers.resourcesync import router as resourcesync_router
from riverhog_api.routers.retrieval import router as retrieval_router
from riverhog_api.routers.search import router as search_router
from riverhog_api.routers.tags import router as tags_router
from riverhog_api.routers.workflows import router as workflows_router
from riverhog_api.schemas.common import ErrorResponse, HealthResponse

_LOG = logging.getLogger(__name__)


def _operation_id(route: APIRoute) -> str:
    return route.name


class _RiverhogAccessLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        path: str | None = None
        status_code: int | None = None
        args = record.args
        if isinstance(args, tuple) and len(args) >= 5:
            path = str(args[2]).split("?", 1)[0]
            try:
                status_code = int(str(args[4]))
            except (TypeError, ValueError):
                status_code = None
        else:
            message = record.getMessage()
            if " /health/live " in message:
                path = "/health/live"
            elif " /health/ready " in message:
                path = "/health/ready"
            if '" 2' in message or '" 3' in message:
                status_code = 200

        if path in {"/health/live", "/health/ready"}:
            return False
        successful = status_code is not None and status_code < 400
        if successful and path is not None:
            if path.startswith("/v1/collection-upload-sessions/") and path.endswith("/files"):
                return False
        return True


def _configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for logger_name in ("riverhog.transfer", "riverhog_api", "riverhog_core"):
        logging.getLogger(logger_name).setLevel(level)
    for logger_name in ("httpx", "httpcore"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(current, _RiverhogAccessLogFilter) for current in access_logger.filters):
        access_logger.addFilter(_RiverhogAccessLogFilter())


def _process_archive_maintenance(
    container: ServiceContainer,
    *,
    startup_recovery: bool = False,
) -> None:
    if startup_recovery:
        requeued_finalizations = (
            container.collection_uploads.requeue_interrupted_finalizations_for_startup(limit=100)
        )
        if requeued_finalizations:
            _LOG.info(
                "startup requeued interrupted collection finalizations: count=%s",
                requeued_finalizations,
            )
        requeued_copies = container.archive_copies.requeue_interrupted_copies_for_startup(limit=100)
        if requeued_copies:
            _LOG.info("startup requeued interrupted archive copies: count=%s", requeued_copies)
        requeued_metadata = (
            container.archive_maintenance.requeue_interrupted_metadata_publications_for_startup()
        )
        if requeued_metadata:
            _LOG.info(
                "startup requeued interrupted metadata-manifest publications: count=%s",
                requeued_metadata,
            )
    container.collection_uploads.process_due_finalizations(limit=1)
    container.collection_workflows.reap_expired_claims(limit=100)
    container.archive_copies.process_due(limit=1)
    container.archive_maintenance.process_due_metadata_publications(limit=10)


def _process_proof_maturations(
    container: ServiceContainer,
    *,
    startup_recovery: bool = False,
) -> None:
    if startup_recovery:
        requeued = container.proof_maturations.requeue_interrupted_for_startup()
        if requeued:
            _LOG.info("startup requeued interrupted proof maturations: count=%s", requeued)
        requeued_attestations = container.archive_attestations.requeue_interrupted_for_startup()
        if requeued_attestations:
            _LOG.info(
                "startup requeued interrupted archive attestations: count=%s",
                requeued_attestations,
            )
    container.proof_maturations.process_due(limit=100)
    container.archive_attestations.process_due(limit=100)


def _abort_incomplete_archive_multipart_uploads(
    container: ServiceContainer,
    *,
    max_age: timedelta,
) -> int:
    initiated_before = utc_now() - max_age
    return container.archive_maintenance.abort_incomplete_multipart_uploads(
        initiated_before=initiated_before
    ) + container.retrieval.abort_incomplete_cache_multipart_uploads(
        initiated_before=initiated_before
    )


async def _run_archive_upload_reaper(
    container_provider: Callable[[], ServiceContainer | None],
    *,
    sweep_interval: timedelta,
    operation_lock: asyncio.Lock,
) -> None:
    interval_seconds = max(sweep_interval.total_seconds(), 0.1)
    startup_recovery = True
    while True:
        try:
            if startup_recovery:
                await asyncio.sleep(0)
            else:
                await asyncio.sleep(interval_seconds)
            container = container_provider()
            if container is None:
                continue
            current_startup_recovery = startup_recovery
            startup_recovery = False
            async with operation_lock:
                await asyncio.to_thread(
                    _process_archive_maintenance,
                    container,
                    startup_recovery=current_startup_recovery,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive background task logging
            _LOG.exception("archive maintenance reaper sweep failed")


async def _run_archive_multipart_reaper(
    container_provider: Callable[[], ServiceContainer | None],
    *,
    sweep_interval: timedelta,
    max_age: timedelta,
    operation_lock: asyncio.Lock,
) -> None:
    interval_seconds = max(sweep_interval.total_seconds(), 0.1)
    first_run = True
    while True:
        try:
            if first_run:
                await asyncio.sleep(0)
            else:
                await asyncio.sleep(interval_seconds)
            first_run = False
            container = container_provider()
            if container is None:
                continue
            async with operation_lock:
                await asyncio.to_thread(
                    _abort_incomplete_archive_multipart_uploads,
                    container,
                    max_age=max_age,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive background task logging
            _LOG.exception("archive multipart upload reaper sweep failed")


async def _run_retrieval_restore_reaper(
    container_provider: Callable[[], ServiceContainer | None],
    *,
    poll_interval: timedelta,
) -> None:
    interval_seconds = max(poll_interval.total_seconds(), 0.1)
    first_run = True
    while True:
        try:
            if first_run:
                await asyncio.sleep(0)
            else:
                await asyncio.sleep(interval_seconds)
            first_run = False
            container = container_provider()
            if container is None:
                continue
            await asyncio.to_thread(container.retrieval.process_due, limit=10)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive background task logging
            _LOG.exception("retrieval restore poll failed")


async def _run_retrieval_cache_reaper(
    container_provider: Callable[[], ServiceContainer | None],
    *,
    sweep_interval: timedelta,
) -> None:
    interval_seconds = max(sweep_interval.total_seconds(), 0.1)
    startup_recovery = True
    while True:
        try:
            if startup_recovery:
                await asyncio.sleep(0)
            else:
                await asyncio.sleep(interval_seconds)
            container = container_provider()
            if container is None:
                continue
            current_startup_recovery = startup_recovery
            startup_recovery = False
            if current_startup_recovery:
                requeued = await asyncio.to_thread(
                    container.retrieval.requeue_interrupted_cache_cleanup_for_startup
                )
                if requeued:
                    _LOG.info(
                        "startup requeued interrupted retrieval-cache cleanup: count=%s",
                        requeued,
                    )
            await asyncio.to_thread(container.retrieval.sweep)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive background task logging
            _LOG.exception("retrieval-cache cleanup sweep failed")


async def _run_proof_maturation_reaper(
    container_provider: Callable[[], ServiceContainer | None],
    *,
    sweep_interval: timedelta,
) -> None:
    interval_seconds = max(sweep_interval.total_seconds(), 0.1)
    startup_recovery = True
    while True:
        try:
            if startup_recovery:
                await asyncio.sleep(0)
            else:
                await asyncio.sleep(interval_seconds)
            container = container_provider()
            if container is None:
                continue
            current_startup_recovery = startup_recovery
            startup_recovery = False
            await asyncio.to_thread(
                _process_proof_maturations,
                container,
                startup_recovery=current_startup_recovery,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive background task logging
            _LOG.exception("proof maturation reaper sweep failed")


def create_app(
    *,
    container: ServiceContainer | None = None,
    container_provider: Callable[[], ServiceContainer] | None = None,
    archive_upload_reaper_interval: float | None = None,
    archive_multipart_reaper_interval: float | None = None,
    retrieval_restore_poll_interval: float | None = None,
    retrieval_cache_reaper_interval: float | None = None,
    proof_maturation_reaper_interval: float | None = None,
) -> FastAPI:
    if container is not None and container_provider is not None:
        raise ValueError("create_app accepts either container or container_provider, not both")

    config = load_runtime_config()
    _configure_logging(config.log_level)
    app_container: ServiceContainer | None = container
    owns_app_container = container is None and container_provider is None
    app_container_lock = threading.Lock()
    archive_sweep_interval = (
        timedelta(seconds=archive_upload_reaper_interval)
        if archive_upload_reaper_interval is not None
        else config.archive_upload_sweep_interval
    )
    archive_multipart_sweep_interval = (
        timedelta(seconds=archive_multipart_reaper_interval)
        if archive_multipart_reaper_interval is not None
        else config.archive_multipart_sweep_interval
    )
    retrieval_poll_interval = (
        timedelta(seconds=retrieval_restore_poll_interval)
        if retrieval_restore_poll_interval is not None
        else config.retrieval_restore_poll_interval
    )
    retrieval_cache_sweep_interval = (
        timedelta(seconds=retrieval_cache_reaper_interval)
        if retrieval_cache_reaper_interval is not None
        else config.retrieval_cache_sweep_interval
    )
    proof_maturation_sweep_interval = (
        timedelta(seconds=proof_maturation_reaper_interval)
        if proof_maturation_reaper_interval is not None
        else config.proof_maturation_sweep_interval
    )

    def get_or_create_container() -> ServiceContainer:
        nonlocal app_container
        if container_provider is not None:
            return container_provider()
        if app_container is None:
            with app_container_lock:
                if app_container is None:
                    app_container = default_container()
        return app_container

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if container_provider is None:
            get_or_create_container()
        archive_operation_lock = asyncio.Lock()
        archive_task = asyncio.create_task(
            _run_archive_upload_reaper(
                get_or_create_container,
                sweep_interval=archive_sweep_interval,
                operation_lock=archive_operation_lock,
            )
        )
        archive_multipart_task = asyncio.create_task(
            _run_archive_multipart_reaper(
                get_or_create_container,
                sweep_interval=archive_multipart_sweep_interval,
                max_age=config.archive_multipart_max_age,
                operation_lock=archive_operation_lock,
            )
        )
        retrieval_restore_task = asyncio.create_task(
            _run_retrieval_restore_reaper(
                get_or_create_container,
                poll_interval=retrieval_poll_interval,
            )
        )
        retrieval_cache_task = asyncio.create_task(
            _run_retrieval_cache_reaper(
                get_or_create_container,
                sweep_interval=retrieval_cache_sweep_interval,
            )
        )
        proof_maturation_task = asyncio.create_task(
            _run_proof_maturation_reaper(
                get_or_create_container,
                sweep_interval=proof_maturation_sweep_interval,
            )
        )
        try:
            yield
        finally:
            archive_task.cancel()
            archive_multipart_task.cancel()
            retrieval_restore_task.cancel()
            retrieval_cache_task.cancel()
            proof_maturation_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await archive_task
            with contextlib.suppress(asyncio.CancelledError):
                await archive_multipart_task
            with contextlib.suppress(asyncio.CancelledError):
                await retrieval_restore_task
            with contextlib.suppress(asyncio.CancelledError):
                await retrieval_cache_task
            with contextlib.suppress(asyncio.CancelledError):
                await proof_maturation_task
            if owns_app_container and app_container is not None:
                app_container.close()
                default_container.cache_clear()

    app = FastAPI(
        title="riverhog API",
        version=importlib.metadata.version("riverhog-server"),
        lifespan=lifespan,
        generate_unique_id_function=_operation_id,
    )
    app.state.public_base_url = config.public_base_url
    app.dependency_overrides[get_container] = get_or_create_container

    @app.exception_handler(RiverhogError)
    async def handle_riverhog_error(_: Request, exc: RiverhogError) -> JSONResponse:
        return JSONResponse(
            status_code=status_for_error_code(exc.code),
            content=error_payload(
                code=exc.code,
                message=exc.message,
                details=exc.details,
            ),
            headers={"WWW-Authenticate": "Bearer"} if exc.code == "unauthorized" else None,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0]
        location = ".".join(str(item) for item in first["loc"] if item not in {"body", "query"})
        message = str(first["msg"])
        if location:
            message = f"{location} {message}"
        return JSONResponse(
            status_code=400,
            content=error_payload(code="bad_request", message=message),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = error_code_for_status(exc.status_code)
        return JSONResponse(
            status_code=status_for_error_code(code, fallback=exc.status_code),
            content=error_payload(code=code, message=str(exc.detail)),
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_: Request, exc: Exception) -> JSONResponse:
        _LOG.error("unhandled Riverhog API error", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content=error_payload(code="internal_error", message="internal server error"),
        )

    @app.get("/health/live", response_model=HealthResponse, tags=["health"])
    async def health_live() -> dict[str, str]:
        return {"service": "riverhog", "status": "ok"}

    @app.get(
        "/health/ready",
        response_model=HealthResponse,
        responses={503: {"model": ErrorResponse}},
        tags=["health"],
    )
    async def health_ready() -> dict[str, str]:
        try:
            get_or_create_container()
        except Exception as exc:
            raise ServiceUnavailable("Riverhog runtime dependencies are not ready") from exc
        return {"service": "riverhog", "status": "ok"}

    app.include_router(collections_router, prefix="/v1")
    app.include_router(events_router, prefix="/v1")
    app.include_router(search_router, prefix="/v1")
    app.include_router(tags_router, prefix="/v1")
    app.include_router(archive_router, prefix="/v1")
    app.include_router(apps_router, prefix="/v1")
    app.include_router(quotas_router, prefix="/v1")
    app.include_router(provenance_router, prefix="/v1")
    app.include_router(retrieval_router, prefix="/v1")
    app.include_router(resourcesync_router)
    app.include_router(workflows_router, prefix="/v1")
    schema = apply_openapi_error_contract(
        app.openapi(),
        operation_error_codes=RIVERHOG_OPERATION_ERROR_CODES,
    )
    app.openapi_schema = apply_openapi_permission_contract(schema, app.routes)
    return app


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="riverhog-api",
        description="Run the Riverhog archive management API.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=importlib.metadata.version("riverhog-server"),
    )
    subparsers = parser.add_subparsers(dest="command")
    state = subparsers.add_parser("state", help="inspect or upgrade the catalog schema")
    state_subparsers = state.add_subparsers(dest="state_command", required=True)
    for command_name, help_text in (
        ("status", "show the current and required catalog revisions"),
        ("upgrade", "explicitly upgrade the catalog to the current revision"),
        ("verify", "verify the current revision and exact catalog schema"),
    ):
        command_parser = state_subparsers.add_parser(command_name, help=help_text)
        command_parser.add_argument("--json", action="store_true", help="Emit JSON.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "state":
        schema = catalog_state_schema(load_runtime_config().database_url)
        try:
            if args.state_command == "status":
                status = schema.status()
            elif args.state_command == "upgrade":
                status = schema.upgrade()
            else:
                status = schema.validate()
        except StateSchemaError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        payload = status.as_dict()
        if args.json:
            print(json.dumps(payload, sort_keys=True))
        else:
            print(
                f"riverhog catalog state: {payload['condition']} "
                f"({payload['current_revision'] or 'none'} -> {payload['head_revision']})"
            )
        return 0
    uvicorn.run(
        "riverhog_api.app:create_app",
        factory=True,
        host="0.0.0.0",
        port=8000,
        reload=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

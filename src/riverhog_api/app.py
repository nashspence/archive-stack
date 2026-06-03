from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
import time
from collections.abc import AsyncIterator, Callable
from datetime import timedelta

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from riverhog_api.auth import api_auth_dependencies
from riverhog_api.deps import ServiceContainer, default_container, get_container
from riverhog_api.routers.collections import router as collections_router
from riverhog_api.routers.dashboard import router as dashboard_router
from riverhog_api.routers.fetches import router as fetches_router
from riverhog_api.routers.files import router as files_router
from riverhog_api.routers.glacier import router as glacier_router
from riverhog_api.routers.images import router as images_router
from riverhog_api.routers.internal import router as internal_router
from riverhog_api.routers.pins import router as pins_router
from riverhog_api.routers.plan import router as plan_router
from riverhog_api.routers.recovery_sessions import router as recovery_sessions_router
from riverhog_api.routers.search import router as search_router
from riverhog_api.schemas.common import ErrorBody, ErrorResponse
from riverhog_core.domain.errors import RiverhogError
from riverhog_core.runtime_config import load_runtime_config

_LOG = logging.getLogger(__name__)


class _RiverhogAccessLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        method: str | None = None
        path: str | None = None
        status_code: int | None = None
        args = record.args
        if isinstance(args, tuple) and len(args) >= 5:
            method = str(args[1])
            path = str(args[2]).split("?", 1)[0]
            try:
                status_code = int(str(args[4]))
            except (TypeError, ValueError):
                status_code = None
        else:
            message = record.getMessage()
            if " /healthz " in message:
                path = "/healthz"
            elif " /internal/tusd/hooks " in message:
                path = "/internal/tusd/hooks"
            elif " /v1/collection-uploads/" in message and "/upload " in message:
                path = "/v1/collection-uploads/.../upload"
            if '" 2' in message or '" 3' in message:
                status_code = 200

        if path == "/healthz":
            return False
        successful = status_code is not None and status_code < 400
        if successful and path == "/internal/tusd/hooks":
            return False
        if (
            successful
            and method == "PATCH"
            and path is not None
            and path.startswith("/v1/collection-uploads/")
            and path.endswith("/upload")
        ):
            return False
        return True


class _RiverhogHttpxLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if (
            message.startswith("HTTP Request: PATCH http://riverhog-tusd:1080/files/")
            and '"HTTP/1.1 204 No Content"' in message
        ):
            return False
        return True


def _configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for logger_name in ("riverhog_api", "riverhog_core"):
        logging.getLogger(logger_name).setLevel(level)
    httpx_logger = logging.getLogger("httpx")
    if not any(isinstance(current, _RiverhogHttpxLogFilter) for current in httpx_logger.filters):
        httpx_logger.addFilter(_RiverhogHttpxLogFilter())
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(current, _RiverhogAccessLogFilter) for current in access_logger.filters):
        access_logger.addFilter(_RiverhogAccessLogFilter())


def _sweep_expired_uploads(container: ServiceContainer) -> None:
    container.collections.expire_stale_uploads()
    container.fetches.expire_stale_uploads()


def _process_glacier_uploads(
    container: ServiceContainer,
    *,
    startup_failed_retry_audit: bool = False,
    startup_recovery_catalog_refresh: bool = False,
) -> None:
    if startup_failed_retry_audit:
        retried = container.glacier_uploads.requeue_failed_uploads_for_startup(limit=100)
        if retried:
            _LOG.info("startup requeued failed collection archive uploads: count=%s", retried)
    if startup_recovery_catalog_refresh:
        archive_count = container.glacier_uploads.publish_recovery_catalog()
        _LOG.info(
            "startup refreshed encrypted archive recovery catalog: archives=%s",
            archive_count,
        )
    container.glacier_uploads.process_due_uploads(limit=1)


def _process_glacier_recovery_sessions(
    container: ServiceContainer,
    *,
    startup_hot_repair_audit: bool = False,
) -> None:
    container.recovery_sessions.repair_missing_pinned_hot_files(
        limit=10_000 if startup_hot_repair_audit else 100
    )
    container.fetches.deliver_due_waiting_notifications(limit=100)
    container.recovery_sessions.process_due_sessions(limit=10)


def _process_planner_refresh(container: ServiceContainer) -> None:
    container.recovery_sessions.repair_missing_pinned_hot_files(limit=100)
    container.fetches.deliver_due_waiting_notifications(limit=100)
    container.planning.process_due_refresh(limit=1)


async def _run_upload_expiry_reaper(
    container_provider: Callable[[], ServiceContainer | None],
    *,
    sweep_interval: timedelta,
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
            await asyncio.to_thread(_sweep_expired_uploads, container)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive background task logging
            _LOG.exception("upload expiry reaper sweep failed")


async def _run_glacier_upload_reaper(
    container_provider: Callable[[], ServiceContainer | None],
    *,
    sweep_interval: timedelta,
) -> None:
    interval_seconds = max(sweep_interval.total_seconds(), 0.1)
    startup_failed_retry_audit = True
    while True:
        try:
            if startup_failed_retry_audit:
                await asyncio.sleep(0)
            else:
                await asyncio.sleep(interval_seconds)
            container = container_provider()
            if container is None:
                continue
            current_startup_failed_retry_audit = startup_failed_retry_audit
            startup_failed_retry_audit = False
            if current_startup_failed_retry_audit:
                _LOG.info(
                    "startup failed archive-upload retry audit and recovery catalog refresh "
                    "queued in background; "
                    "API startup is not blocked"
                )
            await asyncio.to_thread(
                _process_glacier_uploads,
                container,
                startup_failed_retry_audit=current_startup_failed_retry_audit,
                startup_recovery_catalog_refresh=current_startup_failed_retry_audit,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive background task logging
            _LOG.exception("glacier upload reaper sweep failed")


async def _run_glacier_recovery_reaper(
    container_provider: Callable[[], ServiceContainer | None],
    *,
    sweep_interval: timedelta,
) -> None:
    interval_seconds = max(sweep_interval.total_seconds(), 0.1)
    startup_hot_repair_audit = True
    while True:
        try:
            if startup_hot_repair_audit:
                await asyncio.sleep(0)
            else:
                await asyncio.sleep(interval_seconds)
            container = container_provider()
            if container is None:
                continue
            current_startup_hot_repair_audit = startup_hot_repair_audit
            startup_hot_repair_audit = False
            if current_startup_hot_repair_audit:
                _LOG.info(
                    "startup pinned hot-file audit queued in background; API startup is not blocked"
                )
            await asyncio.to_thread(
                _process_glacier_recovery_sessions,
                container,
                startup_hot_repair_audit=current_startup_hot_repair_audit,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive background task logging
            _LOG.exception("glacier recovery reaper sweep failed")


async def _run_planner_refresh_reaper(
    container_provider: Callable[[], ServiceContainer | None],
    *,
    sweep_interval: timedelta,
) -> None:
    interval_seconds = max(sweep_interval.total_seconds(), 0.1)
    first_run = True
    while True:
        try:
            if first_run:
                await asyncio.sleep(0)
            else:
                await asyncio.sleep(interval_seconds)
            current_first_run = first_run
            first_run = False
            container = container_provider()
            if container is None:
                continue
            if current_first_run:
                _LOG.info(
                    "startup planner refresh queued in background; API startup is not blocked"
                )
            await asyncio.to_thread(_process_planner_refresh, container)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive background task logging
            _LOG.exception("planner refresh reaper sweep failed")


def create_app(
    *,
    container: ServiceContainer | None = None,
    container_provider: Callable[[], ServiceContainer] | None = None,
    upload_expiry_reaper_interval: float | None = None,
    glacier_upload_reaper_interval: float | None = None,
    glacier_recovery_reaper_interval: float | None = None,
    planner_refresh_reaper_interval: float | None = None,
) -> FastAPI:
    if container is not None and container_provider is not None:
        raise ValueError("create_app accepts either container or container_provider, not both")

    config = load_runtime_config()
    _configure_logging(config.log_level)
    app_container: ServiceContainer | None = container
    app_container_lock = threading.Lock()
    sweep_interval = (
        timedelta(seconds=upload_expiry_reaper_interval)
        if upload_expiry_reaper_interval is not None
        else config.upload_expiry_sweep_interval
    )
    glacier_sweep_interval = (
        timedelta(seconds=glacier_upload_reaper_interval)
        if glacier_upload_reaper_interval is not None
        else config.glacier_upload_sweep_interval
    )
    glacier_recovery_sweep_interval = (
        timedelta(seconds=glacier_recovery_reaper_interval)
        if glacier_recovery_reaper_interval is not None
        else config.glacier_recovery_sweep_interval
    )
    planner_refresh_sweep_interval = (
        timedelta(seconds=planner_refresh_reaper_interval)
        if planner_refresh_reaper_interval is not None
        else config.planner_refresh_sweep_interval
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
        upload_task = asyncio.create_task(
            _run_upload_expiry_reaper(
                get_or_create_container,
                sweep_interval=sweep_interval,
            )
        )
        glacier_task = asyncio.create_task(
            _run_glacier_upload_reaper(
                get_or_create_container,
                sweep_interval=glacier_sweep_interval,
            )
        )
        glacier_recovery_task = asyncio.create_task(
            _run_glacier_recovery_reaper(
                get_or_create_container,
                sweep_interval=glacier_recovery_sweep_interval,
            )
        )
        planner_refresh_task = asyncio.create_task(
            _run_planner_refresh_reaper(
                get_or_create_container,
                sweep_interval=planner_refresh_sweep_interval,
            )
        )
        try:
            yield
        finally:
            upload_task.cancel()
            glacier_task.cancel()
            glacier_recovery_task.cancel()
            planner_refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await upload_task
            with contextlib.suppress(asyncio.CancelledError):
                await glacier_task
            with contextlib.suppress(asyncio.CancelledError):
                await glacier_recovery_task
            with contextlib.suppress(asyncio.CancelledError):
                await planner_refresh_task

    app = FastAPI(title="riverhog API", version="0.1.0", lifespan=lifespan)
    app.state.instance_id = f"{os.getpid()}-{time.time_ns()}"
    app.dependency_overrides[get_container] = get_or_create_container

    @app.exception_handler(RiverhogError)
    async def handle_riverhog_error(_: Request, exc: RiverhogError) -> JSONResponse:
        status_map = {
            "bad_request": 400,
            "invalid_target": 400,
            "not_found": 404,
            "conflict": 409,
            "invalid_state": 409,
            "hash_mismatch": 409,
            "not_implemented": 501,
            "service_unavailable": 503,
        }
        payload = ErrorResponse(error=ErrorBody(code=exc.code, message=exc.message))
        return JSONResponse(status_code=status_map.get(exc.code, 400), content=payload.model_dump())

    @app.exception_handler(NotImplementedError)
    async def handle_builtin_not_implemented(_: Request, exc: NotImplementedError) -> JSONResponse:
        payload = ErrorResponse(
            error=ErrorBody(code="not_implemented", message=str(exc) or "not implemented")
        )
        return JSONResponse(status_code=501, content=payload.model_dump())

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {
            "status": "ok",
            "instance_id": str(app.state.instance_id),
        }

    auth_deps = list(api_auth_dependencies())
    app.include_router(internal_router)
    app.include_router(files_router, prefix="/v1", dependencies=auth_deps)
    app.include_router(recovery_sessions_router, prefix="/v1", dependencies=auth_deps)
    app.include_router(collections_router, prefix="/v1", dependencies=auth_deps)
    app.include_router(dashboard_router, prefix="/v1", dependencies=auth_deps)
    app.include_router(search_router, prefix="/v1", dependencies=auth_deps)
    app.include_router(plan_router, prefix="/v1", dependencies=auth_deps)
    app.include_router(images_router, prefix="/v1", dependencies=auth_deps)
    app.include_router(glacier_router, prefix="/v1", dependencies=auth_deps)
    app.include_router(pins_router, prefix="/v1", dependencies=auth_deps)
    app.include_router(fetches_router, prefix="/v1", dependencies=auth_deps)
    return app


def main() -> None:
    uvicorn.run("riverhog_api.app:create_app", factory=True, reload=False)


if __name__ == "__main__":
    main()

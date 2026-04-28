from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import AsyncIterator, Callable
from datetime import timedelta

import uvicorn
from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import JSONResponse

from arc_api.auth import api_auth_dependencies
from arc_api.deps import ServiceContainer, default_container, get_container
from arc_api.routers.collections import router as collections_router
from arc_api.routers.fetches import router as fetches_router
from arc_api.routers.files import router as files_router
from arc_api.routers.glacier import router as glacier_router
from arc_api.routers.images import router as images_router
from arc_api.routers.internal import router as internal_router
from arc_api.routers.pins import router as pins_router
from arc_api.routers.plan import router as plan_router
from arc_api.routers.recovery_sessions import router as recovery_sessions_router
from arc_api.routers.search import router as search_router
from arc_api.schemas.common import ErrorBody, ErrorResponse
from arc_core.domain.errors import ArcError
from arc_core.runtime_config import load_runtime_config
from arc_core.sqlite_db import Base, create_sqlite_engine, initialize_db
from arc_core.stores.s3_support import delete_keys_with_prefixes, ensure_bucket_exists

_LOG = logging.getLogger(__name__)
_TEST_CONTROL_ENV = "ARC_ENABLE_TEST_CONTROL"


def _test_control_enabled() -> bool:
    return os.getenv(_TEST_CONTROL_ENV, "0") == "1"


def _terminate_for_restart() -> None:
    # Give the HTTP response a moment to flush before exiting so the caller can
    # reliably observe the restart request succeed.
    time.sleep(0.05)
    os._exit(75)


def _clear_runtime_storage() -> None:
    config = load_runtime_config()
    ensure_bucket_exists(config)
    delete_keys_with_prefixes(
        config,
        [
            "collections/",
            ".arc/uploads/",
            f"{config.glacier_prefix}/",
        ],
    )


def _reset_runtime_state() -> None:
    # Import the catalog models before touching metadata so Base tracks every
    # table the runtime owns.
    from arc_core import catalog_models as _catalog_models  # noqa: PLC0415

    _ = _catalog_models
    config = load_runtime_config()
    _clear_runtime_storage()
    engine = create_sqlite_engine(str(config.sqlite_path))
    try:
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()
    initialize_db(str(config.sqlite_path))


def _sweep_expired_uploads(container: ServiceContainer) -> None:
    container.collections.expire_stale_uploads()
    container.fetches.expire_stale_uploads()


def _process_glacier_uploads(container: ServiceContainer) -> None:
    container.glacier_uploads.process_due_uploads(limit=1)


def _process_glacier_recovery_sessions(container: ServiceContainer) -> None:
    container.recovery_sessions.process_due_sessions(limit=10)


async def _run_upload_expiry_reaper(
    container_provider: Callable[[], ServiceContainer | None],
    *,
    sweep_interval: timedelta,
) -> None:
    interval_seconds = max(sweep_interval.total_seconds(), 0.1)
    while True:
        try:
            await asyncio.sleep(interval_seconds)
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
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            container = container_provider()
            if container is None:
                continue
            await asyncio.to_thread(_process_glacier_uploads, container)
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
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            container = container_provider()
            if container is None:
                continue
            await asyncio.to_thread(_process_glacier_recovery_sessions, container)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive background task logging
            _LOG.exception("glacier recovery reaper sweep failed")


def create_app(
    *,
    container: ServiceContainer | None = None,
    upload_expiry_reaper_interval: float | None = None,
    glacier_upload_reaper_interval: float | None = None,
    glacier_recovery_reaper_interval: float | None = None,
) -> FastAPI:
    config = load_runtime_config()
    app_container: ServiceContainer | None = container
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

    def get_or_create_container() -> ServiceContainer:
        nonlocal app_container
        if app_container is None:
            app_container = default_container()
        return app_container

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        upload_task = asyncio.create_task(
            _run_upload_expiry_reaper(
                lambda: app_container,
                sweep_interval=sweep_interval,
            )
        )
        glacier_task = asyncio.create_task(
            _run_glacier_upload_reaper(
                lambda: app_container,
                sweep_interval=glacier_sweep_interval,
            )
        )
        glacier_recovery_task = asyncio.create_task(
            _run_glacier_recovery_reaper(
                lambda: app_container,
                sweep_interval=glacier_recovery_sweep_interval,
            )
        )
        try:
            yield
        finally:
            upload_task.cancel()
            glacier_task.cancel()
            glacier_recovery_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await upload_task
            with contextlib.suppress(asyncio.CancelledError):
                await glacier_task
            with contextlib.suppress(asyncio.CancelledError):
                await glacier_recovery_task

    app = FastAPI(title="arc API", version="0.1.0", lifespan=lifespan)
    app.state.instance_id = f"{os.getpid()}-{time.time_ns()}"
    app.dependency_overrides[get_container] = get_or_create_container

    @app.exception_handler(ArcError)
    async def handle_arc_error(_: Request, exc: ArcError) -> JSONResponse:
        status_map = {
            "bad_request": 400,
            "invalid_target": 400,
            "not_found": 404,
            "conflict": 409,
            "invalid_state": 409,
            "hash_mismatch": 409,
            "not_implemented": 501,
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

    if _test_control_enabled():

        @app.post("/_test/reset", status_code=204, include_in_schema=False)
        async def reset_under_compose() -> Response:
            await asyncio.to_thread(_reset_runtime_state)
            return Response(status_code=204)

        @app.post("/_test/restart", status_code=202, include_in_schema=False)
        async def restart_under_compose(background_tasks: BackgroundTasks) -> dict[str, str]:
            background_tasks.add_task(_terminate_for_restart)
            return {
                "status": "restarting",
                "instance_id": str(app.state.instance_id),
            }

    auth_deps = list(api_auth_dependencies())
    app.include_router(internal_router)
    app.include_router(files_router, prefix="/v1", dependencies=auth_deps)
    app.include_router(collections_router, prefix="/v1", dependencies=auth_deps)
    app.include_router(search_router, prefix="/v1", dependencies=auth_deps)
    app.include_router(plan_router, prefix="/v1", dependencies=auth_deps)
    app.include_router(images_router, prefix="/v1", dependencies=auth_deps)
    app.include_router(glacier_router, prefix="/v1", dependencies=auth_deps)
    app.include_router(recovery_sessions_router, prefix="/v1", dependencies=auth_deps)
    app.include_router(pins_router, prefix="/v1", dependencies=auth_deps)
    app.include_router(fetches_router, prefix="/v1", dependencies=auth_deps)
    return app


def main() -> None:
    uvicorn.run("arc_api.app:create_app", factory=True, reload=False)


if __name__ == "__main__":
    main()

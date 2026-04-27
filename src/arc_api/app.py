from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import AsyncIterator
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import JSONResponse

from arc_api.auth import api_auth_dependencies
from arc_api.deps import ServiceContainer, default_container, get_container
from arc_api.routers.collections import router as collections_router
from arc_api.routers.fetches import router as fetches_router
from arc_api.routers.files import router as files_router
from arc_api.routers.images import router as images_router
from arc_api.routers.pins import router as pins_router
from arc_api.routers.plan import router as plan_router
from arc_api.routers.search import router as search_router
from arc_api.schemas.common import ErrorBody, ErrorResponse
from arc_core.domain.errors import ArcError
from arc_core.runtime_config import load_runtime_config
from arc_core.sqlite_db import Base, create_sqlite_engine, initialize_db

_LOG = logging.getLogger(__name__)
_TEST_CONTROL_ENV = "ARC_ENABLE_TEST_CONTROL"


def _test_control_enabled() -> bool:
    return os.getenv(_TEST_CONTROL_ENV, "0") == "1"


def _terminate_for_restart() -> None:
    # Give the HTTP response a moment to flush before exiting so the caller can
    # reliably observe the restart request succeed.
    time.sleep(0.05)
    os._exit(75)


def _clear_filer_namespace(filer_url: str) -> None:
    parsed = urlsplit(filer_url)
    prefix = parsed.path.rstrip("/")
    files_to_delete: list[str] = []
    dirs_to_delete: list[str] = []
    seen_dirs: set[str] = set()

    def walk(relpath: str) -> None:
        dir_url = (f"{filer_url.rstrip('/')}/{relpath}".rstrip("/") + "/")
        if dir_url in seen_dirs:
            return
        seen_dirs.add(dir_url)

        with httpx.Client(timeout=5.0) as client:
            response = client.get(
                dir_url,
                params={"recursive": "true", "limit": "100000"},
                headers={"Accept": "application/json"},
            )
            if response.status_code == 404:
                return
            response.raise_for_status()
            payload = response.json()

            for entry in payload.get("Entries") or []:
                full_path = str(entry.get("FullPath", ""))
                child_relpath = full_path.removeprefix(prefix).lstrip("/")
                if not child_relpath:
                    continue
                child_url = (f"{filer_url.rstrip('/')}/{child_relpath}".rstrip("/") + "/")
                child_response = client.get(child_url, headers={"Accept": "application/json"})
                if child_response.status_code == 200:
                    try:
                        child_payload = child_response.json()
                    except ValueError:
                        child_payload = None
                    if isinstance(child_payload, dict) and "Entries" in child_payload:
                        walk(child_relpath)
                        dirs_to_delete.append(child_relpath)
                        continue
                files_to_delete.append(child_relpath)

    walk("")

    with httpx.Client(timeout=5.0) as client:
        for relpath in sorted(files_to_delete, key=lambda item: item.count("/"), reverse=True):
            response = client.delete(f"{filer_url.rstrip('/')}/{relpath.lstrip('/')}")
            if response.status_code not in (200, 204, 404):
                response.raise_for_status()

        for relpath in sorted(set(dirs_to_delete), key=lambda item: item.count("/"), reverse=True):
            response = client.delete(f"{filer_url.rstrip('/')}/{relpath.lstrip('/')}/")
            if response.status_code not in (200, 204, 404):
                response.raise_for_status()


def _reset_runtime_state() -> None:
    # Import the catalog models before touching metadata so Base tracks every
    # table the runtime owns.
    from arc_core import catalog_models as _catalog_models  # noqa: PLC0415

    _ = _catalog_models
    config = load_runtime_config()
    _clear_filer_namespace(config.seaweedfs_filer_url)
    engine = create_sqlite_engine(str(config.sqlite_path))
    try:
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()
    initialize_db(str(config.sqlite_path))


def _sweep_expired_uploads(container: ServiceContainer) -> None:
    container.collections.expire_stale_uploads()
    container.fetches.expire_stale_uploads()


async def _run_upload_expiry_reaper(
    container: ServiceContainer,
    *,
    sweep_interval: timedelta,
) -> None:
    interval_seconds = max(sweep_interval.total_seconds(), 0.1)
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            await asyncio.to_thread(_sweep_expired_uploads, container)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive background task logging
            _LOG.exception("upload expiry reaper sweep failed")


def create_app(
    *,
    container: ServiceContainer | None = None,
    upload_expiry_reaper_interval: float | None = None,
) -> FastAPI:
    config = load_runtime_config()
    app_container = container or default_container()
    sweep_interval = (
        timedelta(seconds=upload_expiry_reaper_interval)
        if upload_expiry_reaper_interval is not None
        else config.upload_expiry_sweep_interval
    )

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(
            _run_upload_expiry_reaper(app_container, sweep_interval=sweep_interval)
        )
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="arc API", version="0.1.0", lifespan=lifespan)
    app.state.instance_id = f"{os.getpid()}-{time.time_ns()}"
    app.dependency_overrides[get_container] = lambda: app_container

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
    app.include_router(files_router, prefix="/v1", dependencies=auth_deps)
    app.include_router(collections_router, prefix="/v1", dependencies=auth_deps)
    app.include_router(search_router, prefix="/v1", dependencies=auth_deps)
    app.include_router(plan_router, prefix="/v1", dependencies=auth_deps)
    app.include_router(images_router, prefix="/v1", dependencies=auth_deps)
    app.include_router(pins_router, prefix="/v1", dependencies=auth_deps)
    app.include_router(fetches_router, prefix="/v1", dependencies=auth_deps)
    return app


def main() -> None:
    uvicorn.run("arc_api.app:create_app", factory=True, reload=False)


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.metadata
import logging
import os
import threading
import time
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import timedelta

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from riverhog_core.runtime_config import load_runtime_config
from riverhog_protocol.errors import RiverhogError
from time_formats import utc_now

from riverhog_api.deps import ServiceContainer, default_container, get_container
from riverhog_api.routers.apps import router as apps_router
from riverhog_api.routers.archive import router as archive_router
from riverhog_api.routers.collections import router as collections_router
from riverhog_api.routers.events import router as events_router
from riverhog_api.routers.internal import router as internal_router
from riverhog_api.routers.quotas import router as quotas_router
from riverhog_api.routers.resourcesync import router as resourcesync_router
from riverhog_api.routers.retrieval import router as retrieval_router
from riverhog_api.routers.search import router as search_router
from riverhog_api.routers.tags import router as tags_router
from riverhog_api.schemas.common import ErrorBody, ErrorResponse

_LOG = logging.getLogger(__name__)


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
            if " /healthz " in message:
                path = "/healthz"
            elif " /internal/tusd/hooks " in message:
                path = "/internal/tusd/hooks"
            if '" 2' in message or '" 3' in message:
                status_code = 200

        if path == "/healthz":
            return False
        successful = status_code is not None and status_code < 400
        if successful and path == "/internal/tusd/hooks":
            return False
        if successful and path is not None:
            if path.startswith("/v1/collection-upload-sessions/") and (
                path.endswith("/files") or path.endswith("/files/upload")
            ):
                return False
            if (
                path.startswith("/v1/collection-upload-sessions/")
                and "/files/" in path
                and path.endswith("/upload")
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
    for logger_name in ("httpx", "httpcore"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(current, _RiverhogAccessLogFilter) for current in access_logger.filters):
        access_logger.addFilter(_RiverhogAccessLogFilter())


def _sweep_expired_uploads(container: ServiceContainer) -> None:
    container.collections.expire_stale_uploads()


def _process_archive_uploads(
    container: ServiceContainer,
    *,
    startup_failed_retry_audit: bool = False,
) -> None:
    if startup_failed_retry_audit:
        retried = container.archive_uploads.requeue_failed_uploads_for_startup(limit=100)
        if retried:
            _LOG.info("startup requeued failed collection archive uploads: count=%s", retried)
        requeued_copies = container.archive_copies.requeue_interrupted_copies_for_startup(limit=100)
        if requeued_copies:
            _LOG.info("startup requeued interrupted archive copies: count=%s", requeued_copies)
        requeued_metadata = (
            container.archive_uploads.requeue_interrupted_metadata_publications_for_startup()
        )
        if requeued_metadata:
            _LOG.info(
                "startup requeued interrupted metadata-manifest publications: count=%s",
                requeued_metadata,
            )
    container.archive_uploads.process_due_uploads(limit=1)
    container.archive_copies.process_due(limit=1)
    container.archive_uploads.process_due_metadata_publications(limit=10)


def _process_ingress_cleanup(
    container: ServiceContainer,
    *,
    startup_recovery: bool = False,
) -> None:
    if startup_recovery:
        requeued = container.archive_uploads.requeue_interrupted_ingress_cleanup_for_startup()
        if requeued:
            _LOG.info("startup requeued interrupted ingress cleanup: count=%s", requeued)
    container.archive_uploads.process_due_ingress_cleanup(limit=100)


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
    return container.archive_uploads.abort_incomplete_multipart_uploads(
        initiated_before=utc_now() - max_age
    )


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


async def _run_archive_upload_reaper(
    container_provider: Callable[[], ServiceContainer | None],
    *,
    sweep_interval: timedelta,
    operation_lock: asyncio.Lock,
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
                    "startup failed archive-upload retry audit queued in background; "
                    "API startup is not blocked"
                )
            async with operation_lock:
                await asyncio.to_thread(
                    _process_archive_uploads,
                    container,
                    startup_failed_retry_audit=current_startup_failed_retry_audit,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive background task logging
            _LOG.exception("archive upload reaper sweep failed")


async def _run_ingress_cleanup_reaper(
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
                _process_ingress_cleanup,
                container,
                startup_recovery=current_startup_recovery,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive background task logging
            _LOG.exception("ingress cleanup reaper sweep failed")


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


async def _run_retrieval_reaper(
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
            await asyncio.to_thread(container.retrieval.process_due, limit=10)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive background task logging
            _LOG.exception("retrieval reaper sweep failed")


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
    upload_expiry_reaper_interval: float | None = None,
    archive_upload_reaper_interval: float | None = None,
    ingress_cleanup_reaper_interval: float | None = None,
    archive_multipart_reaper_interval: float | None = None,
    retrieval_reaper_interval: float | None = None,
    proof_maturation_reaper_interval: float | None = None,
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
    archive_sweep_interval = (
        timedelta(seconds=archive_upload_reaper_interval)
        if archive_upload_reaper_interval is not None
        else config.archive_upload_sweep_interval
    )
    ingress_cleanup_sweep_interval = (
        timedelta(seconds=ingress_cleanup_reaper_interval)
        if ingress_cleanup_reaper_interval is not None
        else config.ingress_cleanup_sweep_interval
    )
    archive_multipart_sweep_interval = (
        timedelta(seconds=archive_multipart_reaper_interval)
        if archive_multipart_reaper_interval is not None
        else config.archive_multipart_sweep_interval
    )
    retrieval_sweep_interval = (
        timedelta(seconds=retrieval_reaper_interval)
        if retrieval_reaper_interval is not None
        else config.retrieval_sweep_interval
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
        upload_task = asyncio.create_task(
            _run_upload_expiry_reaper(
                get_or_create_container,
                sweep_interval=sweep_interval,
            )
        )
        archive_task = asyncio.create_task(
            _run_archive_upload_reaper(
                get_or_create_container,
                sweep_interval=archive_sweep_interval,
                operation_lock=archive_operation_lock,
            )
        )
        ingress_cleanup_task = asyncio.create_task(
            _run_ingress_cleanup_reaper(
                get_or_create_container,
                sweep_interval=ingress_cleanup_sweep_interval,
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
        retrieval_task = asyncio.create_task(
            _run_retrieval_reaper(
                get_or_create_container,
                sweep_interval=retrieval_sweep_interval,
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
            upload_task.cancel()
            archive_task.cancel()
            ingress_cleanup_task.cancel()
            archive_multipart_task.cancel()
            retrieval_task.cancel()
            proof_maturation_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await upload_task
            with contextlib.suppress(asyncio.CancelledError):
                await archive_task
            with contextlib.suppress(asyncio.CancelledError):
                await ingress_cleanup_task
            with contextlib.suppress(asyncio.CancelledError):
                await archive_multipart_task
            with contextlib.suppress(asyncio.CancelledError):
                await retrieval_task
            with contextlib.suppress(asyncio.CancelledError):
                await proof_maturation_task

    app = FastAPI(
        title="riverhog API",
        version=importlib.metadata.version("riverhog-server"),
        lifespan=lifespan,
    )
    app.state.instance_id = f"{os.getpid()}-{time.time_ns()}"
    app.dependency_overrides[get_container] = get_or_create_container

    @app.exception_handler(RiverhogError)
    async def handle_riverhog_error(_: Request, exc: RiverhogError) -> JSONResponse:
        status_map = {
            "bad_request": 400,
            "unauthorized": 401,
            "forbidden": 403,
            "invalid_target": 400,
            "not_found": 404,
            "conflict": 409,
            "invalid_state": 409,
            "hash_mismatch": 409,
            "not_implemented": 501,
            "service_unavailable": 503,
            "download_allowance_exceeded": 429,
        }
        payload = ErrorResponse(error=ErrorBody(code=exc.code, message=exc.message))
        return JSONResponse(
            status_code=status_map.get(exc.code, 400),
            content=payload.model_dump(),
            headers={"WWW-Authenticate": "Bearer"} if exc.code == "unauthorized" else None,
        )

    @app.exception_handler(NotImplementedError)
    async def handle_builtin_not_implemented(_: Request, exc: NotImplementedError) -> JSONResponse:
        payload = ErrorResponse(
            error=ErrorBody(code="not_implemented", message=str(exc) or "not implemented")
        )
        return JSONResponse(status_code=501, content=payload.model_dump())

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, object]:
        return {
            "status": "ok",
            "instance_id": str(app.state.instance_id),
            "ingress_cleanup": get_or_create_container().archive_uploads.ingress_cleanup_status(),
        }

    app.include_router(internal_router)
    app.include_router(collections_router, prefix="/v1")
    app.include_router(events_router, prefix="/v1")
    app.include_router(search_router, prefix="/v1")
    app.include_router(tags_router, prefix="/v1")
    app.include_router(archive_router, prefix="/v1")
    app.include_router(apps_router, prefix="/v1")
    app.include_router(quotas_router, prefix="/v1")
    app.include_router(retrieval_router, prefix="/v1")
    app.include_router(resourcesync_router)
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
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    _parser().parse_args(argv)
    uvicorn.run(
        "riverhog_api.app:create_app",
        factory=True,
        host="0.0.0.0",
        port=8000,
        reload=False,
    )


if __name__ == "__main__":
    main()

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict

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
_TEST_WEBHOOK_CAPTURE_PATH_ENV = "ARC_TEST_WEBHOOK_CAPTURE_PATH"
_DEFAULT_TEST_WEBHOOK_CAPTURE_PATH = "/app/.compose/webhook-captures.jsonl"


class TestWebhookBehavior(TypedDict):
    event: str
    mode: str
    remaining: int
    status_code: int
    delay_seconds: float


def _test_control_enabled() -> bool:
    return os.getenv(_TEST_CONTROL_ENV, "0") == "1"


def _terminate_for_restart() -> None:
    # Give the HTTP response a moment to flush before exiting so the caller can
    # reliably observe the restart request succeed.
    time.sleep(0.05)
    os._exit(75)


def _test_webhook_capture_path() -> Path:
    raw = os.getenv(_TEST_WEBHOOK_CAPTURE_PATH_ENV, _DEFAULT_TEST_WEBHOOK_CAPTURE_PATH).strip()
    return Path(raw).expanduser()


def _isoformat_z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _coerce_int(value: object, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        return int(value)
    raise ValueError(f"cannot coerce {value!r} to int")


def _coerce_float(value: object, *, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        return float(value)
    raise ValueError(f"cannot coerce {value!r} to float")


def _test_webhook_attempt_path() -> Path:
    return _test_webhook_capture_path().with_name("webhook-attempts.jsonl")


def _test_webhook_behavior_path() -> Path:
    return _test_webhook_capture_path().with_name("webhook-behaviors.json")


def _clear_test_webhook_captures() -> None:
    path = _test_webhook_capture_path()
    if path.exists():
        path.unlink()


def _clear_test_webhook_attempts() -> None:
    path = _test_webhook_attempt_path()
    if path.exists():
        path.unlink()


def _clear_test_webhook_behaviors() -> None:
    path = _test_webhook_behavior_path()
    if path.exists():
        path.unlink()


def _append_test_webhook_capture(payload: dict[str, object]) -> None:
    path = _test_webhook_capture_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True))
        handle.write("\n")


def _append_test_webhook_attempt(payload: dict[str, object]) -> None:
    path = _test_webhook_attempt_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True))
        handle.write("\n")


def _load_test_webhook_captures() -> list[dict[str, object]]:
    path = _test_webhook_capture_path()
    if not path.exists():
        return []
    deliveries: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            deliveries.append(payload)
    return deliveries


def _load_test_webhook_attempts() -> list[dict[str, object]]:
    path = _test_webhook_attempt_path()
    if not path.exists():
        return []
    attempts: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            attempts.append(payload)
    return attempts


def _normalize_test_webhook_behavior(
    payload: dict[str, object],
    *,
    minimum_remaining: int = 0,
) -> TestWebhookBehavior | None:
    event = str(payload.get("event", "")).strip()
    if not event:
        return None
    mode = str(payload.get("mode", "status")).strip() or "status"
    if mode not in {"status", "timeout"}:
        return None
    remaining = _coerce_int(payload.get("remaining"), default=1)
    return {
        "event": event,
        "mode": mode,
        "remaining": max(minimum_remaining, remaining),
        "status_code": _coerce_int(payload.get("status_code"), default=503),
        "delay_seconds": max(0.0, _coerce_float(payload.get("delay_seconds"), default=0.0)),
    }


def _load_test_webhook_behaviors() -> list[TestWebhookBehavior]:
    path = _test_webhook_behavior_path()
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        return []
    behaviors: list[TestWebhookBehavior] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        normalized = _normalize_test_webhook_behavior(item)
        if normalized is not None:
            behaviors.append(normalized)
    return behaviors


def _store_test_webhook_behaviors(behaviors: list[TestWebhookBehavior]) -> None:
    path = _test_webhook_behavior_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp_path.write_text(json.dumps(behaviors, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def _append_test_webhook_behavior(behavior: TestWebhookBehavior) -> None:
    behaviors = _load_test_webhook_behaviors()
    behaviors.append(behavior)
    _store_test_webhook_behaviors(behaviors)


def _consume_test_webhook_behavior(event: str) -> TestWebhookBehavior | None:
    behaviors = _load_test_webhook_behaviors()
    matched: TestWebhookBehavior | None = None
    for behavior in behaviors:
        if behavior["event"] != event:
            continue
        remaining = behavior["remaining"]
        if remaining <= 0:
            continue
        matched = {
            "event": behavior["event"],
            "mode": behavior["mode"],
            "remaining": behavior["remaining"],
            "status_code": behavior["status_code"],
            "delay_seconds": behavior["delay_seconds"],
        }
        behavior["remaining"] = remaining - 1
        break
    _store_test_webhook_behaviors(behaviors)
    return matched


def _clear_test_webhook_state() -> None:
    _clear_test_webhook_captures()
    _clear_test_webhook_attempts()
    _clear_test_webhook_behaviors()


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
    _clear_test_webhook_state()


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

        @app.post("/_test/webhooks", status_code=204, include_in_schema=False)
        async def capture_test_webhook(request: Request) -> Response:
            payload = await request.json()
            if not isinstance(payload, dict):
                return Response(status_code=400)
            event = str(payload.get("event", "")).strip()
            behavior = await asyncio.to_thread(_consume_test_webhook_behavior, event)
            mode = behavior["mode"] if behavior is not None else "status"
            delay_seconds = behavior["delay_seconds"] if behavior is not None else 0.0
            status_code = behavior["status_code"] if behavior is not None else 204
            attempt_payload: dict[str, object] = {
                "event": event,
                "payload": payload,
                "received_at": _isoformat_z(datetime.now(UTC)),
                "result": "delivered",
                "status_code": 204,
            }
            if behavior is not None:
                attempt_payload["behavior"] = behavior
            if mode == "timeout":
                attempt_payload["result"] = "timeout"
                attempt_payload["status_code"] = 0
                await asyncio.to_thread(_append_test_webhook_attempt, attempt_payload)
                if delay_seconds > 0:
                    await asyncio.sleep(delay_seconds)
                return Response(status_code=204)
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            if status_code >= 400:
                attempt_payload["result"] = "failed"
                attempt_payload["status_code"] = status_code
                await asyncio.to_thread(_append_test_webhook_attempt, attempt_payload)
                return Response(status_code=status_code)
            await asyncio.to_thread(_append_test_webhook_attempt, attempt_payload)
            await asyncio.to_thread(_append_test_webhook_capture, payload)
            return Response(status_code=204)

        @app.get("/_test/webhooks", include_in_schema=False)
        async def list_test_webhooks() -> dict[str, object]:
            deliveries = await asyncio.to_thread(_load_test_webhook_captures)
            attempts = await asyncio.to_thread(_load_test_webhook_attempts)
            behaviors = await asyncio.to_thread(_load_test_webhook_behaviors)
            return {
                "deliveries": deliveries,
                "attempts": attempts,
                "behaviors": behaviors,
            }

        @app.post("/_test/webhooks/behaviors", status_code=201, include_in_schema=False)
        async def add_test_webhook_behavior(request: Request) -> dict[str, object]:
            payload = await request.json()
            if not isinstance(payload, dict):
                return {"error": "payload must be an object"}
            event = str(payload.get("event", "")).strip()
            if not event:
                return {"error": "event is required"}
            mode = str(payload.get("mode", "status")).strip() or "status"
            if mode not in {"status", "timeout"}:
                return {"error": "mode must be status or timeout"}
            behavior = _normalize_test_webhook_behavior(
                {**payload, "event": event, "mode": mode},
                minimum_remaining=1,
            )
            if behavior is None:
                return {"error": "payload must describe a valid webhook behavior"}
            await asyncio.to_thread(_append_test_webhook_behavior, behavior)
            return {"behavior": behavior}

        @app.delete("/_test/webhooks", status_code=204, include_in_schema=False)
        async def clear_test_webhooks() -> Response:
            await asyncio.to_thread(_clear_test_webhook_state)
            return Response(status_code=204)

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

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Literal, cast
from urllib.parse import parse_qs, urlsplit

from jeb.collector import (
    ATTEMPT_LIST_SORT_FIELDS,
    Collector,
    UnrecoverableJebError,
    event_timestamp,
)

TerminalFilter = Literal["active", "terminal", "all"]
LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class JebServiceOperation:
    id: str
    operation: str
    started_at: str
    account: str | None
    attempt_id: str | None
    thread: threading.Thread

    def summary(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "operation": self.operation,
            "started_at": self.started_at,
        }
        if self.account is not None:
            payload["account"] = self.account
        if self.attempt_id is not None:
            payload["attempt_id"] = self.attempt_id
        return payload


class JebServiceOperations:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: JebServiceOperation | None = None

    def _prune_locked(self) -> None:
        if self._active is not None and not self._active.thread.is_alive():
            self._active = None

    def active_summary(self) -> dict[str, Any] | None:
        with self._lock:
            self._prune_locked()
            return None if self._active is None else self._active.summary()

    def start(
        self,
        *,
        operation: str,
        run: Callable[[], None],
        account: str | None = None,
        attempt_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._prune_locked()
            if self._active is not None:
                active_summary = self._active.summary()
                raise UnrecoverableJebError(
                    "Jeb operation already running: "
                    f"{active_summary['operation']} {active_summary['id']}"
                )
            operation_id = uuid.uuid4().hex[:12]

            def target() -> None:
                try:
                    run()
                except Exception:
                    LOG.exception(
                        "Jeb service operation failed",
                        extra={"operation": operation, "operation_id": operation_id},
                    )

            thread = threading.Thread(
                target=target,
                name=f"jeb-{operation}-{operation_id}",
                daemon=True,
            )
            active_operation = JebServiceOperation(
                id=operation_id,
                operation=operation,
                started_at=event_timestamp(),
                account=account,
                attempt_id=attempt_id,
                thread=thread,
            )
            self._active = active_operation
            thread.start()
            return active_operation.summary()

    def prepare_and_start(
        self,
        *,
        operation: str,
        prepare: Callable[[], str | None],
        run: Callable[[str], None],
        account: str | None = None,
    ) -> tuple[str | None, dict[str, Any] | None]:
        with self._lock:
            self._prune_locked()
            if self._active is not None:
                active_summary = self._active.summary()
                raise UnrecoverableJebError(
                    "Jeb operation already running: "
                    f"{active_summary['operation']} {active_summary['id']}"
                )
            attempt_id = prepare()
            if attempt_id is None:
                return None, None
            operation_id = uuid.uuid4().hex[:12]

            def target() -> None:
                try:
                    run(attempt_id)
                except Exception:
                    LOG.exception(
                        "Jeb service operation failed",
                        extra={"operation": operation, "operation_id": operation_id},
                    )

            thread = threading.Thread(
                target=target,
                name=f"jeb-{operation}-{operation_id}",
                daemon=True,
            )
            active_operation = JebServiceOperation(
                id=operation_id,
                operation=operation,
                started_at=event_timestamp(),
                account=account,
                attempt_id=attempt_id,
                thread=thread,
            )
            self._active = active_operation
            thread.start()
            return attempt_id, active_operation.summary()


@dataclass(frozen=True)
class JebServiceState:
    collector: Collector
    operations: JebServiceOperations = field(default_factory=JebServiceOperations)


def _response(handler: BaseHTTPRequestHandler, status: HTTPStatus, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    handler.send_response(int(status))
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _error(
    handler: BaseHTTPRequestHandler,
    status: HTTPStatus,
    *,
    code: str,
    message: str,
) -> None:
    _response(handler, status, {"error": {"code": code, "message": message}})


def _first(params: dict[str, list[str]], key: str, default: str | None = None) -> str | None:
    values = params.get(key)
    if not values:
        return default
    value = values[0]
    return value if value != "" else default


def _positive_int(params: dict[str, list[str]], key: str, default: int) -> int:
    raw = _first(params, key, str(default))
    try:
        value = int(str(raw))
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if value < 1:
        raise ValueError(f"{key} must be >= 1")
    return value


def _bool(params: dict[str, list[str]], key: str, default: bool) -> bool:
    raw = _first(params, key)
    if raw is None:
        return default
    normalized = raw.casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{key} must be true or false")


def _terminal(params: dict[str, list[str]]) -> TerminalFilter:
    value = _first(params, "terminal", "active") or "active"
    if value not in {"active", "terminal", "all"}:
        raise ValueError("terminal must be active, terminal, or all")
    return cast(TerminalFilter, value)


def _payload_bool(payload: dict[str, Any], key: str, default: bool) -> bool:
    value = payload.get(key, default)
    if isinstance(value, bool):
        return value
    raise ValueError(f"{key} must be true or false")


def _json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("content-length") or "0")
    if length <= 0:
        return {}
    try:
        payload = json.loads(handler.rfile.read(length).decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("request body must be JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    return payload


def _handle_bad_request(handler: BaseHTTPRequestHandler, exc: ValueError) -> None:
    _error(handler, HTTPStatus.BAD_REQUEST, code="bad_request", message=str(exc))


def _handle_invalid_state(handler: BaseHTTPRequestHandler, exc: UnrecoverableJebError) -> None:
    _error(handler, HTTPStatus.CONFLICT, code="invalid_state", message=str(exc))


def jeb_service_handler(state: JebServiceState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            split = urlsplit(self.path)
            params = parse_qs(split.query, keep_blank_values=True)
            try:
                if split.path == "/health/live":
                    _response(self, HTTPStatus.OK, {"service": "jeb", "status": "ok"})
                    return
                if split.path == "/health/ready":
                    _response(
                        self,
                        HTTPStatus.OK,
                        {
                            "service": "jeb",
                            "account_count": len(state.collector.config.accounts),
                            "status": "ok",
                        },
                    )
                    return
                if split.path == "/v1/jeb/config/check":
                    state.collector.init_db()
                    _response(
                        self,
                        HTTPStatus.OK,
                        {
                            "status": "ok",
                            "account_count": len(state.collector.config.accounts),
                            "accounts": [account.id for account in state.collector.config.accounts],
                        },
                    )
                    return
                if split.path == "/v1/jeb/status":
                    state.collector.init_db()
                    payload = state.collector.status_summary(
                        include_backlog=_bool(params, "include_backlog", True)
                    )
                    payload["active_operation"] = state.operations.active_summary()
                    _response(self, HTTPStatus.OK, payload)
                    return
                if split.path == "/v1/jeb/attempts":
                    sort = _first(params, "sort", "updated_at") or "updated_at"
                    if sort not in ATTEMPT_LIST_SORT_FIELDS:
                        raise ValueError(
                            "sort must be one of: " + ", ".join(sorted(ATTEMPT_LIST_SORT_FIELDS))
                        )
                    state.collector.init_db()
                    _response(
                        self,
                        HTTPStatus.OK,
                        state.collector.list_attempts(
                            page=_positive_int(params, "page", 1),
                            per_page=_positive_int(params, "per_page", 25),
                            sort=sort,
                            order=_first(params, "order", "desc") or "desc",
                            query=_first(params, "q") or _first(params, "query"),
                            terminal=_terminal(params),
                            state=_first(params, "state"),
                            account=_first(params, "account"),
                            collection_slug=_first(params, "collection_slug"),
                            target=_first(params, "target"),
                        ),
                    )
                    return
                _response(self, HTTPStatus.NOT_FOUND, {"service": "jeb", "status": "not_found"})
            except ValueError as exc:
                _handle_bad_request(self, exc)

        def do_POST(self) -> None:  # noqa: N802
            split = urlsplit(self.path)
            try:
                if split.path == "/v1/jeb/once":
                    state.collector.init_db()
                    operation_summary = state.operations.start(
                        operation="once",
                        run=state.collector.run_once,
                    )
                    _response(
                        self,
                        HTTPStatus.ACCEPTED,
                        {"status": "started", "operation": operation_summary},
                    )
                    return
                if split.path == "/v1/jeb/archive-now":
                    payload = _json_body(self)
                    account = str(payload.get("account") or "").strip()
                    if not account:
                        raise ValueError("account is required")
                    process = _payload_bool(payload, "process", True)
                    dry_run = _payload_bool(payload, "dry_run", False)
                    state.collector.init_db()
                    if dry_run:
                        _response(
                            self,
                            HTTPStatus.OK,
                            state.collector.archive_plan(account_id=account, process=process),
                        )
                        return
                    archive_operation: dict[str, Any] | None = None
                    if process:
                        attempt_id, archive_operation = state.operations.prepare_and_start(
                            operation="archive-now",
                            account=account,
                            prepare=lambda: state.collector.archive_now(
                                account_id=account,
                                process=False,
                            ),
                            run=state.collector.process_attempt,
                        )
                    else:
                        attempt_id = state.collector.archive_now(account_id=account, process=False)
                    if attempt_id is None:
                        _response(
                            self,
                            HTTPStatus.OK,
                            {"status": "no_eligible_files", "account": account},
                        )
                        return
                    attempt = state.collector.load_attempt(attempt_id)
                    _response(
                        self,
                        HTTPStatus.ACCEPTED,
                        {
                            "status": "started",
                            "account": account,
                            "attempt_id": attempt_id,
                            "batch_id": str(attempt["batch_id"]),
                            "operation": archive_operation,
                        },
                    )
                    return
                _response(self, HTTPStatus.NOT_FOUND, {"service": "jeb", "status": "not_found"})
            except ValueError as exc:
                _handle_bad_request(self, exc)
            except UnrecoverableJebError as exc:
                _handle_invalid_state(self, exc)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return Handler


def start_jeb_service_server(
    host: str,
    port: int,
    state: JebServiceState,
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), jeb_service_handler(state))
    thread = threading.Thread(
        target=server.serve_forever,
        name="jeb-service-api",
        daemon=True,
    )
    thread.start()
    return server

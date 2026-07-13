from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Literal, cast
from urllib.parse import parse_qs, urlsplit

from jeb.collector import BATCH_LIST_SORT_FIELDS, Collector, UnrecoverableJebError

TerminalFilter = Literal["active", "terminal", "all"]


@dataclass(frozen=True)
class JebServiceState:
    collector: Collector


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
                            "source_count": len(state.collector.config.sources),
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
                            "source_count": len(state.collector.config.sources),
                            "sources": [source.id for source in state.collector.config.sources],
                        },
                    )
                    return
                if split.path == "/v1/jeb/status":
                    state.collector.init_db()
                    _response(
                        self,
                        HTTPStatus.OK,
                        state.collector.status_summary(
                            include_backlog=_bool(params, "include_backlog", True)
                        ),
                    )
                    return
                if split.path == "/v1/jeb/batches":
                    sort = _first(params, "sort", "updated_at") or "updated_at"
                    if sort not in BATCH_LIST_SORT_FIELDS:
                        raise ValueError(
                            "sort must be one of: " + ", ".join(sorted(BATCH_LIST_SORT_FIELDS))
                        )
                    state.collector.init_db()
                    _response(
                        self,
                        HTTPStatus.OK,
                        state.collector.list_batches(
                            page=_positive_int(params, "page", 1),
                            per_page=_positive_int(params, "per_page", 25),
                            sort=sort,
                            order=_first(params, "order", "desc") or "desc",
                            query=_first(params, "q") or _first(params, "query"),
                            terminal=_terminal(params),
                            state=_first(params, "state"),
                            account=_first(params, "account"),
                            collection=_first(params, "collection"),
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
                    state.collector.run_once()
                    _response(self, HTTPStatus.ACCEPTED, {"status": "ok"})
                    return
                if split.path == "/v1/jeb/archive-now":
                    payload = _json_body(self)
                    account = str(payload.get("account") or "").strip()
                    if not account:
                        raise ValueError("account is required")
                    process = _payload_bool(payload, "process", True)
                    batch_id = state.collector.archive_now(source_id=account, process=process)
                    if batch_id is None:
                        _response(
                            self,
                            HTTPStatus.OK,
                            {"status": "no_eligible_files", "account": account},
                        )
                        return
                    _response(
                        self,
                        HTTPStatus.ACCEPTED,
                        {"status": "started", "account": account, "batch_id": batch_id},
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

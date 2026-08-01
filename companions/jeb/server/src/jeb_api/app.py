from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Literal, cast
from urllib.parse import parse_qs, urlsplit

from jeb_core.domain.models import UnrecoverableJebError, event_timestamp
from jeb_core.domain.sources import SourceRegistryError
from jeb_core.ingress import (
    JebIngressAuthenticationError,
    JebIngressError,
    authenticate_tus_source,
    prepare_tus_upload,
    publish_tus_upload,
)
from jeb_protocol import ATTEMPT_LIST_SORT_FIELDS

from jeb_api.composition import JebServices

ResolutionFilter = Literal["unresolved", "resolved", "all"]
LOG = logging.getLogger(__name__)
DEFAULT_JEB_API_TOKEN = "jeb-development-api-token"


@dataclass
class JebServiceOperation:
    id: str
    operation: str
    started_at: str
    source: str | None
    attempt_id: str | None
    state: str = "running"
    completed_at: str | None = None
    failure: str | None = None
    thread: threading.Thread | None = None

    def summary(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "operation": self.operation,
            "started_at": self.started_at,
            "state": self.state,
        }
        if self.source is not None:
            payload["source"] = self.source
        if self.attempt_id is not None:
            payload["attempt_id"] = self.attempt_id
        if self.completed_at is not None:
            payload["completed_at"] = self.completed_at
        if self.failure is not None:
            payload["failure"] = self.failure
        return payload


class JebServiceOperations:
    _HISTORY_LIMIT = 100

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_id: str | None = None
        self._operations: dict[str, JebServiceOperation] = {}

    def _prune_locked(self) -> None:
        if self._active_id is None:
            return
        active = self._operations[self._active_id]
        if active.thread is not None and not active.thread.is_alive() and active.state == "running":
            active.state = "failed"
            active.failure = "operation thread ended without reporting a result"
            active.completed_at = event_timestamp()
            self._active_id = None

    def active_summary(self) -> dict[str, Any] | None:
        with self._lock:
            self._prune_locked()
            return None if self._active_id is None else self._operations[self._active_id].summary()

    def get(self, operation_id: str) -> dict[str, Any]:
        with self._lock:
            self._prune_locked()
            operation = self._operations.get(operation_id)
            if operation is None:
                raise KeyError(operation_id)
            return operation.summary()

    def list_page(
        self,
        *,
        page: int,
        per_page: int,
        sort: str,
        order: str,
        query: str | None,
        state: str | None,
        all_items: bool,
    ) -> dict[str, Any]:
        if page < 1 or not 1 <= per_page <= 100:
            raise ValueError("operation page and per_page must be between 1 and 100")
        if sort not in {"id", "operation", "state", "started_at", "completed_at"}:
            raise ValueError("invalid operation sort field")
        if order not in {"asc", "desc"}:
            raise ValueError("order must be asc or desc")
        normalized_query = query.strip().casefold() if query and query.strip() else None
        normalized_state = state.strip().casefold() if state and state.strip() else None
        with self._lock:
            self._prune_locked()
            operations = [current.summary() for current in self._operations.values()]
        if normalized_state is not None:
            operations = [
                current for current in operations if current.get("state") == normalized_state
            ]
        if normalized_query is not None:
            operations = [
                current
                for current in operations
                if normalized_query in " ".join(str(value) for value in current.values()).casefold()
            ]
        operations.sort(
            key=lambda item: (str(item.get(sort) or ""), str(item.get("id") or "")),
            reverse=order == "desc",
        )
        total = len(operations)
        selected = operations if all_items else operations[(page - 1) * per_page : page * per_page]
        return {
            "page": 1 if all_items else page,
            "per_page": total if all_items else per_page,
            "total": total,
            "pages": (1 if total else 0) if all_items else ((total + per_page - 1) // per_page),
            "sort": sort,
            "order": order,
            "query": normalized_query,
            "filters": ({"state": normalized_state} if normalized_state else {}),
            "operations": selected,
        }

    def _start_locked(
        self,
        *,
        operation: str,
        run: Callable[[], None],
        source: str | None,
        attempt_id: str | None,
    ) -> dict[str, Any]:
        operation_id = uuid.uuid4().hex[:12]
        current = JebServiceOperation(
            id=operation_id,
            operation=operation,
            started_at=event_timestamp(),
            source=source,
            attempt_id=attempt_id,
        )

        def target() -> None:
            failure: str | None = None
            try:
                run()
            except Exception as exc:
                failure = f"{type(exc).__name__}: {exc}"
                LOG.exception(
                    "Jeb service operation failed",
                    extra={"operation": operation, "operation_id": operation_id},
                )
            finally:
                with self._lock:
                    stored = self._operations[operation_id]
                    stored.state = "failed" if failure is not None else "succeeded"
                    stored.failure = failure
                    stored.completed_at = event_timestamp()
                    if self._active_id == operation_id:
                        self._active_id = None

        thread = threading.Thread(
            target=target,
            name=f"jeb-{operation}-{operation_id}",
            daemon=True,
        )
        current.thread = thread
        self._operations[operation_id] = current
        self._active_id = operation_id
        while len(self._operations) > self._HISTORY_LIMIT:
            self._operations.pop(next(iter(self._operations)))
        thread.start()
        return current.summary()

    def start(
        self,
        *,
        operation: str,
        run: Callable[[], None],
        source: str | None = None,
        attempt_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._prune_locked()
            if self._active_id is not None:
                active_summary = self._operations[self._active_id].summary()
                raise UnrecoverableJebError(
                    "Jeb operation already running: "
                    f"{active_summary['operation']} {active_summary['id']}"
                )
            return self._start_locked(
                operation=operation,
                run=run,
                source=source,
                attempt_id=attempt_id,
            )

    def prepare_and_start(
        self,
        *,
        operation: str,
        prepare: Callable[[], str | None],
        run: Callable[[str], None],
        source: str | None = None,
    ) -> tuple[str | None, dict[str, Any] | None]:
        with self._lock:
            self._prune_locked()
            if self._active_id is not None:
                active_summary = self._operations[self._active_id].summary()
                raise UnrecoverableJebError(
                    "Jeb operation already running: "
                    f"{active_summary['operation']} {active_summary['id']}"
                )
            attempt_id = prepare()
            if attempt_id is None:
                return None, None
            return attempt_id, self._start_locked(
                operation=operation,
                run=lambda: run(attempt_id),
                source=source,
                attempt_id=attempt_id,
            )


@dataclass(frozen=True)
class JebServiceState:
    services: JebServices
    operations: JebServiceOperations = field(default_factory=JebServiceOperations)
    api_token: str = field(
        default_factory=lambda: os.getenv("JEB_API_TOKEN") or DEFAULT_JEB_API_TOKEN
    )


def _response(handler: BaseHTTPRequestHandler, status: HTTPStatus, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    handler.send_response(int(status))
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _empty_response(
    handler: BaseHTTPRequestHandler,
    status: HTTPStatus,
    *,
    headers: Mapping[str, str] | None = None,
) -> None:
    handler.send_response(int(status))
    for name, value in (headers or {}).items():
        handler.send_header(name, value)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


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


def _optional_bool(params: dict[str, list[str]], key: str) -> bool | None:
    if _first(params, key) is None:
        return None
    return _bool(params, key, False)


def _resolution(params: dict[str, list[str]]) -> ResolutionFilter:
    value = _first(params, "resolution", "unresolved") or "unresolved"
    if value not in {"unresolved", "resolved", "all"}:
        raise ValueError("resolution must be unresolved, resolved, or all")
    return cast(ResolutionFilter, value)


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


def _authorize_management_api(
    handler: BaseHTTPRequestHandler,
    *,
    expected_token: str,
) -> bool:
    scheme, _, supplied = handler.headers.get("Authorization", "").partition(" ")
    if (
        scheme.casefold() == "bearer"
        and supplied
        and secrets.compare_digest(supplied, expected_token)
    ):
        return True
    _empty_response(
        handler,
        HTTPStatus.UNAUTHORIZED,
        headers={"WWW-Authenticate": "Bearer"},
    )
    return False


def _tus_rejection(status: HTTPStatus, message: str) -> dict[str, Any]:
    return {
        "RejectUpload": True,
        "HTTPResponse": {
            "StatusCode": int(status),
            "Body": message,
            "Header": {"Content-Type": "text/plain"},
        },
    }


def _source_path(path: str, *, action: str | None = None) -> str | None:
    prefix = "/v1/sources/"
    if not path.startswith(prefix):
        return None
    remainder = path.removeprefix(prefix)
    if action is None:
        return remainder if remainder and "/" not in remainder else None
    suffix = f"/{action}"
    if not remainder.endswith(suffix):
        return None
    source_id = remainder.removesuffix(suffix)
    return source_id if source_id and "/" not in source_id else None


def _attempt_path(path: str) -> str | None:
    prefix = "/v1/attempts/"
    if not path.startswith(prefix):
        return None
    attempt_id = path.removeprefix(prefix)
    return attempt_id if attempt_id and "/" not in attempt_id else None


def _operation_path(path: str) -> str | None:
    prefix = "/v1/operations/"
    if not path.startswith(prefix):
        return None
    operation_id = path.removeprefix(prefix)
    return operation_id if operation_id and "/" not in operation_id else None


def _sequence(payload: Mapping[str, Any], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a JSON array")
    return [str(item) for item in value]


def _mapping(payload: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} must be a JSON object")
    return dict(value)


def jeb_service_handler(state: JebServiceState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            split = urlsplit(self.path)
            params = parse_qs(split.query, keep_blank_values=True)
            try:
                if split.path == "/internal/ingress/tus/auth":
                    state.services.runtime.initialize()
                    try:
                        authenticate_tus_source(
                            state.services.source_registry,
                            self.headers.get("Authorization"),
                        )
                    except JebIngressAuthenticationError:
                        _empty_response(
                            self,
                            HTTPStatus.UNAUTHORIZED,
                            headers={"WWW-Authenticate": 'Basic realm="Jeb ingress"'},
                        )
                    else:
                        _empty_response(self, HTTPStatus.NO_CONTENT)
                    return
                if split.path == "/health/live":
                    _response(self, HTTPStatus.OK, {"service": "jeb", "status": "ok"})
                    return
                if split.path == "/health/ready":
                    state.services.runtime.initialize()
                    _response(
                        self,
                        HTTPStatus.OK,
                        {
                            "service": "jeb",
                            "source_count": state.services.source_registry.count(),
                            "status": "ok",
                        },
                    )
                    return
                if not _authorize_management_api(self, expected_token=state.api_token):
                    return
                if split.path == "/v1/events":
                    state.services.runtime.initialize()
                    page = state.services.event_log.page(
                        after=_first(params, "after"),
                        limit=_positive_int(params, "limit", 100),
                    )
                    _response(self, HTTPStatus.OK, page.model_dump(mode="json"))
                    return
                if split.path == "/v1/config/check":
                    state.services.runtime.initialize()
                    _response(
                        self,
                        HTTPStatus.OK,
                        {
                            "status": "ok",
                            "source_count": state.services.source_registry.count(),
                        },
                    )
                    return
                if split.path == "/v1/operations":
                    _response(
                        self,
                        HTTPStatus.OK,
                        state.operations.list_page(
                            page=_positive_int(params, "page", 1),
                            per_page=_positive_int(params, "per_page", 25),
                            sort=_first(params, "sort", "started_at") or "started_at",
                            order=_first(params, "order", "desc") or "desc",
                            query=_first(params, "q") or _first(params, "query"),
                            state=_first(params, "state"),
                            all_items=_bool(params, "all", False),
                        ),
                    )
                    return
                operation_id = _operation_path(split.path)
                if operation_id is not None:
                    try:
                        payload = state.operations.get(operation_id)
                    except KeyError:
                        _error(
                            self,
                            HTTPStatus.NOT_FOUND,
                            code="not_found",
                            message=f"Jeb operation not found: {operation_id}",
                        )
                    else:
                        _response(self, HTTPStatus.OK, payload)
                    return
                if split.path == "/v1/sources":
                    state.services.runtime.initialize()
                    _response(
                        self,
                        HTTPStatus.OK,
                        state.services.source_registry.list_page(
                            page=_positive_int(params, "page", 1),
                            per_page=_positive_int(params, "per_page", 25),
                            sort=_first(params, "sort", "id") or "id",
                            order=_first(params, "order", "asc") or "asc",
                            query=_first(params, "q") or _first(params, "query"),
                            enabled=_optional_bool(params, "enabled"),
                            adapter=_first(params, "adapter"),
                            target=_first(params, "target"),
                            all_items=_bool(params, "all", False),
                        ),
                    )
                    return
                source_id = _source_path(split.path)
                if source_id is not None:
                    state.services.runtime.initialize()
                    _response(
                        self,
                        HTTPStatus.OK,
                        state.services.source_registry.get(source_id).summary(),
                    )
                    return
                if split.path == "/v1/status":
                    state.services.runtime.initialize()
                    payload = state.services.runtime.status_summary(
                        include_backlog=_bool(params, "include_backlog", True)
                    )
                    payload["active_operation"] = state.operations.active_summary()
                    _response(self, HTTPStatus.OK, payload)
                    return
                if split.path == "/v1/attempts":
                    sort = _first(params, "sort", "updated_at") or "updated_at"
                    if sort not in ATTEMPT_LIST_SORT_FIELDS:
                        raise ValueError(
                            "sort must be one of: " + ", ".join(sorted(ATTEMPT_LIST_SORT_FIELDS))
                        )
                    state.services.runtime.initialize()
                    _response(
                        self,
                        HTTPStatus.OK,
                        state.services.store.list_attempts(
                            page=_positive_int(params, "page", 1),
                            per_page=_positive_int(params, "per_page", 25),
                            sort=sort,
                            order=_first(params, "order", "desc") or "desc",
                            query=_first(params, "q") or _first(params, "query"),
                            resolution=_resolution(params),
                            state=_first(params, "state"),
                            source=_first(params, "source"),
                            target=_first(params, "target"),
                            all_items=_bool(params, "all", False),
                        ),
                    )
                    return
                attempt_id = _attempt_path(split.path)
                if attempt_id is not None:
                    state.services.runtime.initialize()
                    try:
                        payload = state.services.store.get_attempt(attempt_id)
                    except KeyError:
                        _error(
                            self,
                            HTTPStatus.NOT_FOUND,
                            code="not_found",
                            message=f"Jeb attempt not found: {attempt_id}",
                        )
                    else:
                        _response(self, HTTPStatus.OK, payload)
                    return
                _response(self, HTTPStatus.NOT_FOUND, {"service": "jeb", "status": "not_found"})
            except ValueError as exc:
                _handle_bad_request(self, exc)

        def do_POST(self) -> None:  # noqa: N802
            split = urlsplit(self.path)
            try:
                if split.path == "/internal/ingress/tus/hooks":
                    payload = _json_body(self)
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
                            prepared = prepare_tus_upload(
                                state.services.config.ingress,
                                state.services.source_registry,
                                authorization=self.headers.get("Authorization"),
                                metadata=metadata,
                                size=upload.get("Size"),
                            )
                        except JebIngressAuthenticationError as exc:
                            _response(
                                self,
                                HTTPStatus.OK,
                                _tus_rejection(HTTPStatus.UNAUTHORIZED, str(exc)),
                            )
                            return
                        except JebIngressError as exc:
                            _response(
                                self,
                                HTTPStatus.OK,
                                _tus_rejection(HTTPStatus.BAD_REQUEST, str(exc)),
                            )
                            return
                        _response(
                            self,
                            HTTPStatus.OK,
                            {
                                "ChangeFileInfo": {
                                    "ID": prepared.upload_id,
                                    "MetaData": prepared.hook_metadata(),
                                }
                            },
                        )
                        return
                    if hook_type == "post-finish":
                        publish_tus_upload(
                            state.services.config.ingress,
                            state.services.source_registry,
                            upload=upload,
                        )
                    _response(self, HTTPStatus.OK, {})
                    return
                if not _authorize_management_api(self, expected_token=state.api_token):
                    return
                if split.path == "/v1/sources":
                    payload = _json_body(self)
                    source_id = str(payload.get("id") or "").strip()
                    if not source_id:
                        raise ValueError("id is required")
                    source_config, credential = state.services.sources.add_source(
                        source_id,
                        adapters=_sequence(payload, "adapters"),
                        target_config=_mapping(payload, "target_config"),
                        credential=(
                            str(payload["credential"])
                            if payload.get("credential") is not None
                            else None
                        ),
                        enabled=_payload_bool(payload, "enabled", True),
                        stable_seconds=int(payload.get("stable_seconds", 600)),
                        include_extensions=(
                            _sequence(payload, "include_extensions")
                            if "include_extensions" in payload
                            else ()
                        ),
                        target=str(payload.get("target") or "munchy"),
                        threshold_bytes=int(payload.get("threshold_bytes", 0)),
                        cleanup=cast(
                            Literal["never", "after_target_success"],
                            str(payload.get("cleanup") or "after_target_success"),
                        ),
                        cadence=cast(
                            Literal["weekly", "monthly", "seasonal", "manual"],
                            str(payload.get("cadence") or "weekly"),
                        ),
                        weekday=int(payload.get("weekday", 0)),
                        hour=int(payload.get("hour", 3)),
                        minute=int(payload.get("minute", 0)),
                    )
                    created_payload: dict[str, Any] = {"source": source_config.summary()}
                    if credential is not None:
                        created_payload["credential"] = credential
                    _response(self, HTTPStatus.CREATED, created_payload)
                    return
                for action, enabled in (("enable", True), ("disable", False)):
                    selected_source = _source_path(split.path, action=action)
                    if selected_source is not None:
                        state.services.runtime.initialize()
                        source_config = state.services.source_registry.set_enabled(
                            selected_source,
                            enabled,
                        )
                        _response(self, HTTPStatus.OK, source_config.summary())
                        return
                selected_source = _source_path(split.path, action="credential")
                if selected_source is not None:
                    payload = _json_body(self)
                    state.services.runtime.initialize()
                    source_config, credential = state.services.source_registry.rotate_credential(
                        selected_source,
                        credential=(
                            str(payload["credential"])
                            if payload.get("credential") is not None
                            else None
                        ),
                    )
                    credential_payload: dict[str, Any] = {"source": source_config.summary()}
                    if credential is not None:
                        credential_payload["credential"] = credential
                    _response(self, HTTPStatus.OK, credential_payload)
                    return
                selected_source = _source_path(split.path, action="removal-plan")
                if selected_source is not None:
                    if state.operations.active_summary() is not None:
                        raise UnrecoverableJebError(
                            "a Jeb operation is running; request the removal plan again later"
                        )
                    payload = _json_body(self)
                    _response(
                        self,
                        HTTPStatus.OK,
                        state.services.sources.source_removal_plan(
                            selected_source,
                            purge=_payload_bool(payload, "purge", False),
                        ),
                    )
                    return
                if split.path == "/v1/once":
                    state.services.runtime.initialize()
                    operation_summary = state.operations.start(
                        operation="once",
                        run=state.services.runtime.run_once,
                    )
                    _response(
                        self,
                        HTTPStatus.ACCEPTED,
                        {"status": "started", "operation": operation_summary},
                    )
                    return
                if split.path == "/v1/archive-now":
                    payload = _json_body(self)
                    source = str(payload.get("source") or "").strip()
                    if not source:
                        raise ValueError("source is required")
                    process = _payload_bool(payload, "process", True)
                    dry_run = _payload_bool(payload, "dry_run", False)
                    state.services.runtime.initialize()
                    if dry_run:
                        _response(
                            self,
                            HTTPStatus.OK,
                            state.services.attempts.archive_plan(source_id=source, process=process),
                        )
                        return
                    archive_operation: dict[str, Any] | None = None
                    if process:
                        attempt_id, archive_operation = state.operations.prepare_and_start(
                            operation="archive-now",
                            source=source,
                            prepare=lambda: state.services.attempts.archive_now(
                                source_id=source,
                                process=False,
                            ),
                            run=state.services.attempts.process_attempt,
                        )
                    else:
                        attempt_id = state.services.attempts.archive_now(
                            source_id=source, process=False
                        )
                    if attempt_id is None:
                        _response(
                            self,
                            HTTPStatus.OK,
                            {"status": "no_eligible_files", "source": source},
                        )
                        return
                    attempt = state.services.store.load_attempt(attempt_id)
                    _response(
                        self,
                        HTTPStatus.ACCEPTED,
                        {
                            "status": "started",
                            "source": source,
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
            except JebIngressError as exc:
                _error(
                    self,
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    code="ingress_failed",
                    message=str(exc),
                )

        def do_PATCH(self) -> None:  # noqa: N802
            split = urlsplit(self.path)
            if not _authorize_management_api(self, expected_token=state.api_token):
                return
            try:
                source_id = _source_path(split.path)
                if source_id is None:
                    _response(
                        self,
                        HTTPStatus.NOT_FOUND,
                        {"service": "jeb", "status": "not_found"},
                    )
                    return
                payload = _json_body(self)
                source = state.services.sources.update_source(source_id, payload)
                _response(self, HTTPStatus.OK, source.summary())
            except (SourceRegistryError, ValueError) as exc:
                _handle_bad_request(self, exc)
            except UnrecoverableJebError as exc:
                _handle_invalid_state(self, exc)

        def do_DELETE(self) -> None:  # noqa: N802
            split = urlsplit(self.path)
            if not _authorize_management_api(self, expected_token=state.api_token):
                return
            try:
                attempt_id = _attempt_path(split.path)
                if attempt_id is not None:
                    state.services.runtime.initialize()
                    try:
                        payload = state.services.attempts.cancel_attempt(attempt_id)
                    except KeyError:
                        _error(
                            self,
                            HTTPStatus.NOT_FOUND,
                            code="not_found",
                            message=f"Jeb attempt not found: {attempt_id}",
                        )
                    else:
                        _response(self, HTTPStatus.OK, payload)
                    return
                source_id = _source_path(split.path)
                if source_id is None:
                    _response(
                        self,
                        HTTPStatus.NOT_FOUND,
                        {"service": "jeb", "status": "not_found"},
                    )
                    return
                if state.operations.active_summary() is not None:
                    raise UnrecoverableJebError(
                        "a Jeb operation is running; source removal cannot begin"
                    )
                payload = _json_body(self)
                challenge = str(payload.get("challenge") or "")
                _response(
                    self,
                    HTTPStatus.OK,
                    state.services.sources.remove_source(source_id, challenge=challenge),
                )
            except (SourceRegistryError, ValueError) as exc:
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

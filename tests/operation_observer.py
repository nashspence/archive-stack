from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any

from fastapi import FastAPI, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient


class TimeoutNeutralTestClient:
    """Adapt TestClient to official clients that always pass production timeouts."""

    def __init__(
        self,
        client: TestClient,
        *,
        observer: OperationObserver | None = None,
    ) -> None:
        self._client = client
        self._observer = observer

    def request(self, method: str, path: str, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.pop("timeout", None)
        started = time.perf_counter()
        response = self._client.request(method, path, **kwargs)
        if self._observer is not None:
            self._observer.record_client_response(
                response,
                elapsed_ms=(time.perf_counter() - started) * 1000,
            )
        return response

    def get(self, path: str, **kwargs):  # type: ignore[no-untyped-def]
        return self.request("GET", path, **kwargs)

    def head(self, path: str, **kwargs):  # type: ignore[no-untyped-def]
        return self.request("HEAD", path, **kwargs)

    def post(self, path: str, **kwargs):  # type: ignore[no-untyped-def]
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs):  # type: ignore[no-untyped-def]
        return self.request("PUT", path, **kwargs)

    def patch(self, path: str, **kwargs):  # type: ignore[no-untyped-def]
        return self.request("PATCH", path, **kwargs)

    @contextmanager
    def stream(self, method: str, path: str, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.pop("timeout", None)
        started = time.perf_counter()
        with self._client.stream(method, path, **kwargs) as response:
            if self._observer is not None:
                self._observer.record_client_response(
                    response,
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                )
            yield response

    def close(self) -> None:
        self._client.close()


@dataclass(slots=True)
class OperationObserver:
    """Record successful real-ASGI operation witnesses and server wall times."""

    application: str
    successful: set[str] = field(default_factory=set)
    server_wall_ms: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    client_wall_ms: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))

    @classmethod
    def install(cls, app: FastAPI, *, application: str) -> OperationObserver:
        observer = cls(application=application)
        _OBSERVERS.append(observer)

        @app.middleware("http")
        async def observe_operation(request: Request, call_next):  # type: ignore[no-untyped-def]
            started = time.perf_counter()
            response = await call_next(request)
            elapsed_ms = (time.perf_counter() - started) * 1000
            route = request.scope.get("route")
            operation_id = (
                (route.operation_id or route.name) if isinstance(route, APIRoute) else None
            )
            if operation_id and response.status_code < 400:
                observer.successful.add(operation_id)
                observer.server_wall_ms[operation_id].append(elapsed_ms)
                response.headers["X-Riverhog-Qualification-Operation"] = operation_id
            return response

        return observer

    def record_client_response(self, response: Any, *, elapsed_ms: float) -> None:
        operation_id = response.headers.get("X-Riverhog-Qualification-Operation", "")
        if operation_id:
            self.client_wall_ms[str(operation_id)].append(elapsed_ms)

    def record_external_server(self, operation_id: str, *, elapsed_ms: float) -> None:
        """Record a successful gateway/protocol operation outside FastAPI."""

        assert elapsed_ms > 0
        self.successful.add(operation_id)
        self.server_wall_ms[operation_id].append(elapsed_ms)

    def record_external_client(self, operation_id: str, *, elapsed_ms: float) -> None:
        """Record the official-client wall time for an external operation."""

        assert elapsed_ms > 0
        self.client_wall_ms[operation_id].append(elapsed_ms)

    def require(self, operation_ids: Iterable[str]) -> None:
        expected = set(operation_ids)
        missing = sorted(expected - self.successful)
        assert not missing, f"operations lack a successful real-API witness: {missing}"
        assert all(
            sample > 0 for operation_id in expected for sample in self.server_wall_ms[operation_id]
        )


_OBSERVERS: list[OperationObserver] = []
_CLI_CALLBACKS: dict[object, list[tuple[str, str]]] = defaultdict(list)
_CLI_PROJECTIONS: dict[tuple[str, str], dict[bool, int]] = defaultdict(lambda: defaultdict(int))
_PREVIOUS_PROFILE: Any = None


def _install_cli_projection_observer() -> None:
    """Observe real command callbacks in both supported output projections."""

    global _PREVIOUS_PROFILE

    from scripts.operation_qualification import application_surfaces, operation_matrix

    required = {
        (item.application, command)
        for item in operation_matrix()
        if item.classification == "human-cli+json"
        for command in item.cli_commands
    }
    for surface in application_surfaces():
        for command, callback, _parity in surface.cli_commands:
            identity = (surface.name, command)
            if identity in required:
                _CLI_CALLBACKS[callback.__code__].append(identity)
    _PREVIOUS_PROFILE = sys.getprofile()
    sys.setprofile(_observe_cli_projection)


def _observe_cli_projection(frame: Any, event: str, argument: object) -> None:
    del argument
    identities = _CLI_CALLBACKS.get(frame.f_code) if event == "call" else None
    if not identities:
        return
    local = frame.f_locals
    if "json_mode" in local:
        json_mode = bool(local["json_mode"])
    elif "args" in local:
        json_mode = bool(getattr(local["args"], "json", False))
    elif "context" in local:
        json_mode = bool(getattr(getattr(local["context"], "obj", None), "json_output", False))
    else:
        return
    if len(identities) > 1 and "args" in local:
        enabled = bool(getattr(local["args"], "enabled", True))
        suffix = "enable" if enabled else "disable"
        identities = [identity for identity in identities if identity[1].endswith(suffix)]
    for identity in identities:
        _CLI_PROJECTIONS[identity][json_mode] += 1


def _timing_summary(samples: list[float]) -> dict[str, int | float]:
    return {
        "samples": len(samples),
        "minimum_ms": round(min(samples), 3),
        "median_ms": round(median(samples), 3),
        "maximum_ms": round(max(samples), 3),
    }


def timing_evidence(*, source_sha: str, exit_status: int) -> dict[str, object]:
    if len(source_sha) != 40 or any(
        character not in "0123456789abcdef" for character in source_sha
    ):
        raise ValueError("operation timing evidence requires an exact lowercase source SHA")
    operations: list[dict[str, object]] = []
    for observer in _OBSERVERS:
        for operation_id in sorted(observer.successful):
            item: dict[str, object] = {
                "application": observer.application,
                "operation_id": operation_id,
                "server_wall": _timing_summary(observer.server_wall_ms[operation_id]),
            }
            client_samples = observer.client_wall_ms.get(operation_id)
            if client_samples:
                item["client_wall"] = _timing_summary(client_samples)
            operations.append(item)
    return {
        "schema": "riverhog-operation-timings/v1",
        "source_sha": source_sha,
        "pytest_exit_status": exit_status,
        "operations": operations,
        "cli_projections": [
            {
                "application": application,
                "command": command,
                "human_executions": modes[False],
                "json_executions": modes[True],
            }
            for (application, command), modes in sorted(_CLI_PROJECTIONS.items())
        ],
    }


def pytest_sessionstart(session: Any) -> None:
    del session
    if os.getenv("RIVERHOG_OPERATION_TIMINGS"):
        _install_cli_projection_observer()


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    del session
    output = os.getenv("RIVERHOG_OPERATION_TIMINGS")
    if not output:
        return
    sys.setprofile(_PREVIOUS_PROFILE)
    payload = timing_evidence(
        source_sha=os.environ.get("RIVERHOG_OPERATION_SOURCE_SHA", ""),
        exit_status=exitstatus,
    )
    destination = Path(output).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

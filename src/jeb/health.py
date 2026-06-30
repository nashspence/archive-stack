from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass(frozen=True)
class JebHealthState:
    source_count: int


def _response(handler: BaseHTTPRequestHandler, status: HTTPStatus, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    handler.send_response(int(status))
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def health_handler(state: JebHealthState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health/live":
                _response(
                    self,
                    HTTPStatus.OK,
                    {
                        "service": "jeb",
                        "status": "ok",
                    },
                )
                return
            if self.path == "/health/ready":
                _response(
                    self,
                    HTTPStatus.OK,
                    {
                        "service": "jeb",
                        "source_count": state.source_count,
                        "status": "ok",
                    },
                )
                return
            _response(
                self,
                HTTPStatus.NOT_FOUND,
                {
                    "service": "jeb",
                    "status": "not_found",
                },
            )

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return Handler


def start_health_server(host: str, port: int, state: JebHealthState) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), health_handler(state))
    thread = threading.Thread(
        target=server.serve_forever,
        name="jeb-health",
        daemon=True,
    )
    thread.start()
    return server

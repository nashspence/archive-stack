#!/usr/bin/env python3
"""Smoke-test the final Mango Fish image against disposable HTTP peers and state."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

IMAGE = os.environ.get("MANGO_FISH_IMAGE", "mango-fish:dev")
CONTAINER_HOST = "host.docker.internal"
SOURCE_TOKEN = "mango-fish-smoke-token"
WEBHOOK_SECRET = "mango-fish-smoke-webhook-secret"


class SmokePeer:
    def __init__(self) -> None:
        self.requested_after: list[str] = []
        self.deliveries: list[dict[str, Any]] = []

    def handler(self) -> type[BaseHTTPRequestHandler]:
        peer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
                request = urlsplit(self.path)
                if request.path != "/events":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                if self.headers.get("Authorization") != f"Bearer {SOURCE_TOKEN}":
                    self.send_error(HTTPStatus.UNAUTHORIZED)
                    return
                after = parse_qs(request.query).get("after", [""])[0]
                peer.requested_after.append(after)
                events = []
                next_cursor = after
                if after == "0":
                    events = [
                        {
                            "specversion": "1.0",
                            "id": "mango-fish-smoke-event",
                            "source": "urn:riverhog:smoke",
                            "type": "io.riverhog.smoke",
                            "time": "2026-08-14T00:00:00.000000Z",
                            "datacontenttype": "application/json",
                            "data": {"proof": "container-entrypoint"},
                        }
                    ]
                    next_cursor = "1"
                self._json_response(
                    {
                        "events": events,
                        "next_cursor": next_cursor,
                        "has_more": False,
                    }
                )

            def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
                request = urlsplit(self.path)
                if request.path != f"/hooks/{WEBHOOK_SECRET}":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                if parse_qs(request.query).get("token") != [WEBHOOK_SECRET]:
                    self.send_error(HTTPStatus.UNAUTHORIZED)
                    return
                if self.headers.get("Content-Type") != "application/cloudevents+json":
                    self.send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    self.send_error(HTTPStatus.BAD_REQUEST)
                    return
                peer.deliveries.append(payload)
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _json_response(self, payload: dict[str, Any]) -> None:
                encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        return Handler


def _run_container(config: Path, state: Path, port: int, *args: str) -> None:
    command = [
        "docker",
        "run",
        "--rm",
        "--add-host",
        f"{CONTAINER_HOST}:host-gateway",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--volume",
        f"{config}:/config/mango-fish.yaml:ro",
        "--volume",
        f"{state}:/state",
        "--env",
        f"EVENT_TOKEN={SOURCE_TOKEN}",
        "--env",
        (
            "SMOKE_WEBHOOK_URL="
            f"http://{CONTAINER_HOST}:{port}/hooks/{WEBHOOK_SECRET}?token={WEBHOOK_SECRET}"
        ),
        IMAGE,
        "--config",
        "/config/mango-fish.yaml",
        *args,
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    output = completed.stdout + completed.stderr
    if SOURCE_TOKEN in output or WEBHOOK_SECRET in output:
        raise RuntimeError("Mango Fish container output exposed a configured credential")
    if completed.returncode != 0:
        safe_output = output.replace(SOURCE_TOKEN, "<redacted>").replace(
            WEBHOOK_SECRET, "<redacted>"
        )
        raise RuntimeError(
            f"Mango Fish container exited {completed.returncode}: {safe_output.strip()}"
        )


def main() -> int:
    peer = SmokePeer()
    server = ThreadingHTTPServer(("0.0.0.0", 0), peer.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = int(server.server_address[1])
        with tempfile.TemporaryDirectory(prefix="riverhog-mango-fish-smoke-") as scratch:
            root = Path(scratch)
            config = root / "mango-fish.yaml"
            state = root / "state"
            state.mkdir()
            config.write_text(
                "\n".join(
                    (
                        "version: 1",
                        "state_path: /state/mango-fish.sqlite3",
                        "poll_interval_seconds: 0.1",
                        "request_timeout_seconds: 5",
                        "batch_size: 100",
                        "sources:",
                        "  - name: smoke",
                        f"    events_url: http://{CONTAINER_HOST}:{port}/events",
                        "    token_env: EVENT_TOKEN",
                        "    webhook_url_env: SMOKE_WEBHOOK_URL",
                        "",
                    )
                ),
                encoding="utf-8",
            )

            _run_container(config, state, port, "state", "upgrade", "--json")
            _run_container(config, state, port, "--once")
            _run_container(config, state, port, "--once")
            _run_container(config, state, port, "state", "verify", "--json")

            with sqlite3.connect(state / "mango-fish.sqlite3") as connection:
                cursor = connection.execute(
                    "SELECT cursor FROM source_cursors WHERE source = ?", ("smoke",)
                ).fetchone()

            if peer.requested_after != ["0", "1"]:
                raise RuntimeError(f"unexpected source cursors: {peer.requested_after!r}")
            if [delivery.get("id") for delivery in peer.deliveries] != ["mango-fish-smoke-event"]:
                raise RuntimeError("Mango Fish did not deliver exactly one expected CloudEvent")
            if cursor != ("1",):
                raise RuntimeError(f"Mango Fish did not persist the expected cursor: {cursor!r}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    print("Mango Fish final-image smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

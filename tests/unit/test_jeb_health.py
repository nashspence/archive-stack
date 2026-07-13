from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from jeb.collector import Collector, config_from_env
from jeb.service_api import JebServiceState, start_jeb_service_server


def read_json(url: str) -> dict[str, object]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def post_json(url: str, payload: dict[str, Any]) -> tuple[int, dict[str, object]]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def jeb_env(tmp_path: Path) -> dict[str, str]:
    return {
        "JEB_ACCOUNTS": "phone",
        "JEB_LANDING_DIR": str(tmp_path / "landing"),
        "JEB_STATE_DIR": str(tmp_path / "state"),
        "JEB_MUNCHY_URL": "http://munchy.invalid",
        "JEB_INCLUDE_EXTENSIONS": ".txt",
        "JEB_STABLE_AGE": "0s",
        "JEB_CADENCE": "weekly",
    }


def write_stable_file(path: Path, content: bytes = b"notes") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    old = time.time() - 14 * 86_400
    os.utime(path, (old, old))


def test_jeb_service_api_reports_live_ready_and_status(tmp_path: Path) -> None:
    env = jeb_env(tmp_path)
    write_stable_file(tmp_path / "landing" / "phone" / "note.txt")
    collector = Collector(config_from_env(env))
    collector.init_db()
    server = start_jeb_service_server("127.0.0.1", 0, JebServiceState(collector=collector))
    try:
        host, port = server.server_address[:2]

        assert read_json(f"http://{host}:{port}/health/live") == {
            "service": "jeb",
            "status": "ok",
        }
        assert read_json(f"http://{host}:{port}/health/ready") == {
            "service": "jeb",
            "source_count": 1,
            "status": "ok",
        }
        status = read_json(f"http://{host}:{port}/v1/jeb/status")
        sources = status["sources"]
        assert isinstance(sources, list)
        assert sources[0]["id"] == "phone"
        assert sources[0]["eligible_files"] == 1
    finally:
        server.shutdown()
        server.server_close()


def test_jeb_service_api_requires_boolean_archive_now_process_flag(tmp_path: Path) -> None:
    collector = Collector(config_from_env(jeb_env(tmp_path)))
    server = start_jeb_service_server("127.0.0.1", 0, JebServiceState(collector=collector))
    try:
        host, port = server.server_address[:2]

        status, payload = post_json(
            f"http://{host}:{port}/v1/jeb/archive-now",
            {"account": "phone", "process": "false"},
        )

        assert status == 400
        assert payload == {
            "error": {"code": "bad_request", "message": "process must be true or false"}
        }
    finally:
        server.shutdown()
        server.server_close()


def test_jeb_service_api_archive_now_dry_run_does_not_create_batch(tmp_path: Path) -> None:
    env = jeb_env(tmp_path)
    write_stable_file(tmp_path / "landing" / "phone" / "note.txt")
    processed = threading.Event()

    class FailingRunner:
        def advance(self, collector: Collector, batch_id: str) -> None:
            processed.set()
            raise AssertionError("dry-run must not process a batch")

    collector = Collector(config_from_env(env), target_runners={"munchy": FailingRunner()})
    server = start_jeb_service_server("127.0.0.1", 0, JebServiceState(collector=collector))
    try:
        host, port = server.server_address[:2]

        status, payload = post_json(
            f"http://{host}:{port}/v1/jeb/archive-now",
            {"account": "phone", "dry_run": True},
        )

        assert status == 200
        assert payload["status"] == "would_process"
        assert payload["dry_run"] is True
        assert payload["account"] == "phone"
        assert payload["file_count"] == 1
        assert payload["total_bytes"] == 5
        assert payload["batch_id"]
        assert payload["job_id"]
        assert collector.active_batch_ids() == []
        assert collector.list_batches(terminal="all")["total"] == 0
        assert not processed.is_set()
    finally:
        server.shutdown()
        server.server_close()


def test_jeb_service_api_archive_now_processes_in_background(tmp_path: Path) -> None:
    env = jeb_env(tmp_path)
    write_stable_file(tmp_path / "landing" / "phone" / "note.txt")
    processed = threading.Event()
    processed_batch_ids: list[str] = []

    class CompleteRunner:
        def advance(self, collector: Collector, batch_id: str) -> None:
            processed_batch_ids.append(batch_id)
            collector.set_batch_state(batch_id, "target_complete")
            processed.set()

    collector = Collector(config_from_env(env), target_runners={"munchy": CompleteRunner()})
    server = start_jeb_service_server("127.0.0.1", 0, JebServiceState(collector=collector))
    try:
        host, port = server.server_address[:2]

        status, payload = post_json(
            f"http://{host}:{port}/v1/jeb/archive-now",
            {"account": "phone"},
        )

        assert status == 202
        assert payload["status"] == "started"
        assert payload["account"] == "phone"
        assert payload["batch_id"]
        operation = payload["operation"]
        assert isinstance(operation, dict)
        assert operation["operation"] == "archive-now"
        assert operation["account"] == "phone"
        assert operation["batch_id"] == payload["batch_id"]
        assert processed.wait(timeout=5)
        assert processed_batch_ids == [payload["batch_id"]]
    finally:
        server.shutdown()
        server.server_close()


def test_jeb_service_api_unknown_paths_are_not_healthy(tmp_path: Path) -> None:
    collector = Collector(config_from_env(jeb_env(tmp_path)))
    server = start_jeb_service_server("127.0.0.1", 0, JebServiceState(collector=collector))
    try:
        host, port = server.server_address[:2]

        try:
            read_json(f"http://{host}:{port}/not-health")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
            assert json.loads(exc.read().decode("utf-8")) == {
                "service": "jeb",
                "status": "not_found",
            }
        else:
            raise AssertionError("unknown Jeb health paths must return 404")
    finally:
        server.shutdown()
        server.server_close()

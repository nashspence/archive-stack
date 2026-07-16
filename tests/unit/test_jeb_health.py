from __future__ import annotations

import base64
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


def post_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object]]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
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


def basic_authorization(account: str, password: str) -> str:
    encoded = base64.b64encode(f"{account}:{password}".encode()).decode()
    return f"Basic {encoded}"


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
            "account_count": 1,
            "status": "ok",
        }
        status = read_json(f"http://{host}:{port}/v1/jeb/status")
        accounts = status["accounts"]
        assert isinstance(accounts, list)
        assert accounts[0]["id"] == "phone"
        assert accounts[0]["eligible_files"] == 1
        assert status["incomplete_tus_uploads"] == {
            "total": 0,
            "bytes": 0,
            "oldest_age_seconds": 0,
            "stale": 0,
            "stale_bytes": 0,
            "max_age_seconds": 14 * 86_400,
            "invalid_records": 0,
            "scan_error": None,
        }
    finally:
        server.shutdown()
        server.server_close()


def test_jeb_tus_ingress_authenticates_and_publishes_completed_file(tmp_path: Path) -> None:
    env = {
        **jeb_env(tmp_path),
        "JEB_TUS_ACCOUNTS": "phone",
        "JEB_ACCOUNT_PHONE_PASSWORD": "phone-password",
    }
    collector = Collector(config_from_env(env))
    server = start_jeb_service_server("127.0.0.1", 0, JebServiceState(collector=collector))
    try:
        host, port = server.server_address[:2]
        authorization = basic_authorization("phone", "phone-password")
        auth_request = urllib.request.Request(
            f"http://{host}:{port}/internal/ingress/tus/auth",
            headers={"Authorization": authorization},
        )
        with urllib.request.urlopen(auth_request, timeout=5) as response:
            assert response.status == 204

        status, prepared = post_json(
            f"http://{host}:{port}/internal/ingress/tus/hooks",
            {
                "Type": "pre-create",
                "Event": {
                    "Upload": {
                        "Size": 5,
                        "Offset": 0,
                        "MetaData": {"filename": "notes/note.txt"},
                    }
                },
            },
            headers={"Authorization": authorization},
        )
        assert status == 200
        change = prepared["ChangeFileInfo"]
        assert isinstance(change, dict)
        upload_id = str(change["ID"])
        metadata = change["MetaData"]
        assert isinstance(metadata, dict)

        staging = collector.config.ingress.tus_staging_dir
        staging.mkdir(parents=True)
        source = staging / upload_id
        info = staging / f"{upload_id}.info"
        source.write_bytes(b"notes")
        info.write_text("{}", encoding="utf-8")
        assert collector.eligible_files(collector.account_by_id("phone")) == []

        finished = {
            "Type": "post-finish",
            "Event": {
                "Upload": {
                    "ID": upload_id,
                    "Size": 5,
                    "Offset": 5,
                    "MetaData": metadata,
                    "Storage": {
                        "Type": "filestore",
                        "Path": str(source),
                        "InfoPath": str(info),
                    },
                }
            },
        }
        assert post_json(
            f"http://{host}:{port}/internal/ingress/tus/hooks",
            finished,
        ) == (200, {})
        assert post_json(
            f"http://{host}:{port}/internal/ingress/tus/hooks",
            finished,
        ) == (200, {})

        destination = tmp_path / "landing" / "phone" / "notes" / "note.txt"
        assert destination.read_bytes() == b"notes"
        assert not source.exists()
        assert not info.exists()
        assert [
            item.rel.as_posix()
            for item in collector.eligible_files(collector.account_by_id("phone"))
        ] == ["notes/note.txt"]
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
        def advance(self, collector: Collector, attempt_id: str) -> None:
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
        assert collector.active_attempt_ids() == []
        assert collector.list_attempts(terminal="all")["total"] == 0
        assert not processed.is_set()
    finally:
        server.shutdown()
        server.server_close()


def test_jeb_service_api_archive_now_processes_in_background(tmp_path: Path) -> None:
    env = jeb_env(tmp_path)
    write_stable_file(tmp_path / "landing" / "phone" / "note.txt")
    processed = threading.Event()
    processed_attempt_ids: list[str] = []

    class CompleteRunner:
        def advance(self, collector: Collector, attempt_id: str) -> None:
            processed_attempt_ids.append(attempt_id)
            collector.set_attempt_state(attempt_id, "target_complete")
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
        assert payload["attempt_id"]
        operation = payload["operation"]
        assert isinstance(operation, dict)
        assert operation["operation"] == "archive-now"
        assert operation["account"] == "phone"
        assert operation["attempt_id"] == payload["attempt_id"]
        assert processed.wait(timeout=5)
        assert processed_attempt_ids == [payload["attempt_id"]]
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

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

import jeb_core.adapters.munchy as munchy_adapter_module
import pytest
from jeb_api.app import JebServiceState, start_jeb_service_server
from jeb_api.composition import JebServices, config_from_env, create_services
from jeb_core.adapters.munchy import MunchyTargetAdapter

API_HEADERS = {"Authorization": "Bearer jeb-development-api-token"}


def read_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, object]:
    request = urllib.request.Request(
        url,
        headers=API_HEADERS if headers is None else headers,
    )
    with urllib.request.urlopen(request, timeout=5) as response:
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
        headers={
            "Content-Type": "application/json",
            **(API_HEADERS if headers is None else headers),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def request_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object]]:
    request = urllib.request.Request(
        url,
        data=None if payload is None else json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            **(API_HEADERS if headers is None else headers),
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def jeb_env(tmp_path: Path) -> dict[str, str]:
    return {
        "JEB_LANDING_DIR": str(tmp_path / "landing"),
        "JEB_STATE_DIR": str(tmp_path / "state"),
        "JEB_MUNCHY_URL": "http://munchy.invalid",
        "JEB_FTP_UID": str(os.getuid()),
        "JEB_FTP_GID": str(os.getgid()),
    }


def services_for(
    env: dict[str, str],
    *,
    target_adapters=None,
) -> JebServices:
    services = create_services(config_from_env(env), target_adapters=target_adapters)
    services.sources.add_source(
        "phone",
        adapters=("tus",),
        target_config={"template_id": "camera-archive"},
        credential="phone-password",
        stable_seconds=0,
        include_extensions=(".txt",),
    )
    return services


@pytest.fixture(autouse=True)
def accept_target_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeMunchyClient:
        def __init__(self, url: str, *, token: str = "") -> None:
            self.url = url
            self.token = token

        def preflight_submission(self, request: object) -> dict[str, object]:
            _ = request
            return {"accepted": True}

        def close(self) -> None:
            pass

    monkeypatch.setattr(munchy_adapter_module, "MunchyClient", FakeMunchyClient)


def write_stable_file(path: Path, content: bytes = b"notes") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    old = time.time() - 14 * 86_400
    os.utime(path, (old, old))


def basic_authorization(source: str, password: str) -> str:
    encoded = base64.b64encode(f"{source}:{password}".encode()).decode()
    return f"Basic {encoded}"


def test_jeb_service_api_reports_live_ready_and_status(tmp_path: Path) -> None:
    env = jeb_env(tmp_path)
    write_stable_file(tmp_path / "landing" / "phone" / "note.txt")
    services = services_for(env)
    server = start_jeb_service_server("127.0.0.1", 0, JebServiceState(services=services))
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
        status = read_json(f"http://{host}:{port}/v1/status")
        sources = status["sources"]
        assert isinstance(sources, list)
        assert sources[0]["id"] == "phone"
        assert sources[0]["eligible_files"] == 1
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


def test_jeb_management_api_requires_its_own_bearer_token(tmp_path: Path) -> None:
    services = services_for(jeb_env(tmp_path))
    server = start_jeb_service_server("127.0.0.1", 0, JebServiceState(services=services))
    try:
        host, port = server.server_address[:2]
        request = urllib.request.Request(f"http://{host}:{port}/v1/status")
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(request, timeout=5)
        assert denied.value.code == 401
        assert denied.value.headers["WWW-Authenticate"] == "Bearer"
    finally:
        server.shutdown()
        server.server_close()


def test_jeb_management_api_gets_one_attempt_by_list_identity(tmp_path: Path) -> None:
    env = jeb_env(tmp_path)
    write_stable_file(tmp_path / "landing" / "phone" / "note.txt")
    services = services_for(env)
    services.runtime.initialize()
    attempt_id = services.attempts.archive_now(source_id="phone", process=False)
    assert attempt_id is not None
    server = start_jeb_service_server("127.0.0.1", 0, JebServiceState(services=services))
    try:
        host, port = server.server_address[:2]
        attempt = read_json(f"http://{host}:{port}/v1/attempts/{attempt_id}")
        assert attempt["attempt_id"] == attempt_id
        assert attempt["source_id"] == "phone"
        assert attempt["file_count"] == 1

        unresolved = read_json(f"http://{host}:{port}/v1/attempts?resolution=unresolved")
        assert unresolved["resolution"] == "unresolved"
        assert unresolved["attempts"][0]["attempt_id"] == attempt_id

        status, missing = request_json(
            "GET",
            f"http://{host}:{port}/v1/attempts/missing",
        )
        assert status == 404
        assert missing["error"] == {
            "code": "not_found",
            "message": "Jeb attempt not found: missing",
        }
    finally:
        server.shutdown()
        server.server_close()


def test_jeb_service_api_manages_source_lifecycle(tmp_path: Path) -> None:
    services = create_services(config_from_env(jeb_env(tmp_path)))
    services.runtime.initialize()
    server = start_jeb_service_server("127.0.0.1", 0, JebServiceState(services=services))
    try:
        host, port = server.server_address[:2]
        base = f"http://{host}:{port}/v1/sources"
        status, created = request_json(
            "POST",
            base,
            {
                "id": "phone",
                "adapters": ["ftp", "tus"],
                "target_config": {"template_id": "camera-archive"},
                "credential": "phone-password",
                "cadence": "manual",
            },
        )
        assert status == 201
        assert "credential" not in created

        listed = read_json(base)
        assert [source["id"] for source in listed["sources"]] == ["phone"]
        assert listed["page"] == 1
        assert listed["per_page"] == 25
        assert listed["total"] == 1
        assert listed["pages"] == 1
        assert listed["sources"][0]["target_config"] == {"template_id": "camera-archive"}
        filtered = read_json(
            f"{base}?q=PHO&enabled=true&adapter=tus&target=munchy"
            "&sort=updated_at&order=desc&all=true"
        )
        assert filtered["total"] == 1
        assert filtered["page"] == 1
        assert filtered["per_page"] == 1
        assert filtered["filters"] == {
            "enabled": True,
            "adapter": "tus",
            "target": "munchy",
        }
        status, disabled = request_json("POST", f"{base}/phone/disable", {})
        assert status == 200
        assert disabled["enabled"] is False
        status, updated = request_json(
            "PATCH",
            f"{base}/phone",
            {"cadence": "weekly"},
        )
        assert status == 200
        assert updated["cadence"] == "weekly"
        status, rotated = request_json(
            "POST",
            f"{base}/phone/credential",
            {"credential": "replacement-password"},
        )
        assert status == 200
        assert "credential" not in rotated

        status, plan = request_json("POST", f"{base}/phone/removal-plan", {"purge": False})
        assert status == 200
        assert plan["status"] == "ready"
        status, removed = request_json(
            "DELETE",
            f"{base}/phone",
            {"challenge": plan["challenge"]},
        )
        assert status == 200
        assert removed == {
            "status": "removed",
            "source": "phone",
            "purged": False,
            "files": 0,
            "bytes": 0,
        }
    finally:
        server.shutdown()
        server.server_close()


def test_jeb_tus_ingress_authenticates_and_publishes_completed_file(tmp_path: Path) -> None:
    services = services_for(jeb_env(tmp_path))
    server = start_jeb_service_server("127.0.0.1", 0, JebServiceState(services=services))
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

        staging = services.config.ingress.tus_staging_dir
        staging.mkdir(parents=True)
        source = staging / upload_id
        info = staging / f"{upload_id}.info"
        source.write_bytes(b"notes")
        info.write_text("{}", encoding="utf-8")
        assert services.sources.eligible_files(services.sources.source_by_id("phone")) == []

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
            for item in services.sources.eligible_files(services.sources.source_by_id("phone"))
        ] == ["notes/note.txt"]
    finally:
        server.shutdown()
        server.server_close()


def test_jeb_service_api_requires_boolean_archive_now_process_flag(tmp_path: Path) -> None:
    services = services_for(jeb_env(tmp_path))
    server = start_jeb_service_server("127.0.0.1", 0, JebServiceState(services=services))
    try:
        host, port = server.server_address[:2]

        status, payload = post_json(
            f"http://{host}:{port}/v1/archive-now",
            {"source": "phone", "process": "false"},
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

    class FailingAdapter(MunchyTargetAdapter):
        def advance(self, services: JebServices, attempt_id: str) -> None:
            processed.set()
            raise AssertionError("dry-run must not process a batch")

    services = services_for(env, target_adapters={"munchy": FailingAdapter()})
    server = start_jeb_service_server("127.0.0.1", 0, JebServiceState(services=services))
    try:
        host, port = server.server_address[:2]

        status, payload = post_json(
            f"http://{host}:{port}/v1/archive-now",
            {"source": "phone", "dry_run": True},
        )

        assert status == 200
        assert payload["status"] == "would_process"
        assert payload["dry_run"] is True
        assert payload["source"] == "phone"
        assert payload["file_count"] == 1
        assert payload["total_bytes"] == 5
        assert payload["batch_id"]
        assert payload["target_submission_id"]
        assert services.store.unresolved_attempt_ids() == []
        assert services.store.list_attempts(resolution="all")["total"] == 0
        assert not processed.is_set()
    finally:
        server.shutdown()
        server.server_close()


def test_jeb_service_api_archive_now_processes_in_background(tmp_path: Path) -> None:
    env = jeb_env(tmp_path)
    write_stable_file(tmp_path / "landing" / "phone" / "note.txt")
    processed = threading.Event()
    processed_attempt_ids: list[str] = []

    class CompleteAdapter(MunchyTargetAdapter):
        def advance(self, services: JebServices, attempt_id: str) -> None:
            processed_attempt_ids.append(attempt_id)
            services.store.set_attempt_state(attempt_id, "target_complete")
            processed.set()

    services = services_for(env, target_adapters={"munchy": CompleteAdapter()})
    server = start_jeb_service_server("127.0.0.1", 0, JebServiceState(services=services))
    try:
        host, port = server.server_address[:2]

        status, payload = post_json(
            f"http://{host}:{port}/v1/archive-now",
            {"source": "phone"},
        )

        assert status == 202
        assert payload["status"] == "started"
        assert payload["source"] == "phone"
        assert payload["batch_id"]
        assert payload["attempt_id"]
        operation = payload["operation"]
        assert isinstance(operation, dict)
        assert operation["operation"] == "archive-now"
        assert operation["source"] == "phone"
        assert operation["attempt_id"] == payload["attempt_id"]
        assert processed.wait(timeout=5)
        assert processed_attempt_ids == [payload["attempt_id"]]
        operation_id = str(operation["id"])
        deadline = time.monotonic() + 5
        while True:
            shown = read_json(f"http://{host}:{port}/v1/operations/{operation_id}")
            if shown["state"] != "running":
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert shown["state"] == "succeeded"
        page = read_json(f"http://{host}:{port}/v1/operations?all=true")
        assert page["total"] == 1
        assert page["operations"] == [shown]
    finally:
        server.shutdown()
        server.server_close()


def test_jeb_service_api_cancels_an_unresolved_attempt(tmp_path: Path) -> None:
    env = jeb_env(tmp_path)
    write_stable_file(tmp_path / "landing" / "phone" / "note.txt")
    services = services_for(env)
    server = start_jeb_service_server("127.0.0.1", 0, JebServiceState(services=services))
    try:
        host, port = server.server_address[:2]
        status, started = post_json(
            f"http://{host}:{port}/v1/archive-now",
            {"source": "phone", "process": False},
        )
        assert status == 202
        attempt_id = str(started["attempt_id"])

        status, canceled = request_json(
            "DELETE",
            f"http://{host}:{port}/v1/attempts/{attempt_id}",
        )

        assert status == 200
        assert canceled["state"] == "canceled"
        assert read_json(f"http://{host}:{port}/v1/attempts?resolution=resolved")["total"] == 1
    finally:
        server.shutdown()
        server.server_close()


def test_jeb_service_api_unknown_paths_are_not_healthy(tmp_path: Path) -> None:
    services = services_for(jeb_env(tmp_path))
    server = start_jeb_service_server("127.0.0.1", 0, JebServiceState(services=services))
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

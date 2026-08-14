from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import httpx
import jeb_core.adapters.munchy as munchy_adapter_module
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from jeb_api.app import JebServiceState, create_app, start_jeb_service_server
from jeb_api.composition import JebServices, config_from_env, create_services
from jeb_api_client import JebApiClient, JebIngressClient
from jeb_core.adapters.munchy import MunchyTargetAdapter
from jeb_core.persistence.schema import upgrade_state
from jeb_core.provenance import put_ingress_binding
from riverhog_provenance import (
    FileProvenanceBinding,
    build_portable_provenance_set,
    create_observation_journal,
    validate_journal,
)

from tests.operation_observer import OperationObserver, TimeoutNeutralTestClient

API_HEADERS = {"Authorization": "Bearer test-jeb-management-token"}


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
        "JEB_API_TOKEN": "test-jeb-management-token",
        "JEB_LANDING_DIR": str(tmp_path / "landing"),
        "JEB_STATE_DIR": str(tmp_path / "state"),
        "JEB_MUNCHY_URL": "https://munchy.invalid",
        "JEB_FTP_UID": str(os.getuid()),
        "JEB_FTP_GID": str(os.getgid()),
    }


def services_for(
    env: dict[str, str],
    *,
    target_adapters=None,
) -> JebServices:
    services = create_services(config_from_env(env), target_adapters=target_adapters)
    upgrade_state(services.config)
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
        def __init__(
            self,
            url: str,
            *,
            token: str = "",
            allow_insecure_http: bool = False,
        ) -> None:
            self.url = url
            self.token = token
            self.allow_insecure_http = allow_insecure_http

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


def test_jeb_official_client_covers_complete_positive_api_lifecycle(tmp_path: Path) -> None:
    env = jeb_env(tmp_path)
    services = create_services(config_from_env(env))
    upgrade_state(services.config)
    application = create_app(JebServiceState(services=services))
    observer = OperationObserver.install(application, application="jeb")
    transport = TestClient(application, headers=API_HEADERS)
    client = JebApiClient(
        "http://testserver",
        token="test-jeb-management-token",
        allow_insecure_http=True,
    )
    client._client = TimeoutNeutralTestClient(  # type: ignore[assignment]
        transport,
        observer=observer,
    )

    assert transport.get("/health/live", headers={}).status_code == 200
    assert transport.get("/health/ready", headers={}).status_code == 200
    created = client.create_source(
        {
            "id": "qualification",
            "adapters": ["tus"],
            "target_config": {"template_id": "camera-archive"},
            "credential": "qualification-password",
            "stable_seconds": 0,
            "include_extensions": [".txt"],
            "cadence": "manual",
        }
    )
    assert created["source"]["id"] == "qualification"
    assert client.list_sources(query="qualification", all_items=True)["total"] == 1
    assert client.get_source("qualification")["id"] == "qualification"
    assert client.update_source("qualification", {"cadence": "weekly"})["cadence"] == "weekly"
    assert client.disable_source("qualification")["enabled"] is False
    assert client.enable_source("qualification")["enabled"] is True
    rotated = client.rotate_source_credential(
        "qualification",
        credential="qualification-password-2",
    )
    assert rotated["source"]["id"] == "qualification"
    assert client.check_config()["status"] == "ok"
    assert client.get_status(include_backlog=True)["sources"][0]["id"] == "qualification"

    source = tmp_path / "landing" / "qualification" / "notes" / "note.txt"
    write_stable_file(source)
    archived = client.archive_source_now(source="qualification", process=False)
    attempt_id = str(archived["attempt_id"])
    assert client.list_attempts(source="qualification", all_items=True)["total"] == 1
    assert client.get_attempt(attempt_id)["attempt_id"] == attempt_id
    assert client.cancel_attempt(attempt_id)["state"] == "canceled"

    once = client.run_once()
    operation_id = str(once["operation"]["id"])
    deadline = time.monotonic() + 5
    while True:
        operation = client.get_operation(operation_id)
        if operation["state"] != "running":
            break
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert operation["state"] == "succeeded"
    assert client.list_operations(all_items=True)["total"] >= 1

    source = tmp_path / "client-upload" / "note.txt"
    write_stable_file(source)
    journal = create_observation_journal(
        source,
        relative_path="notes/note.txt",
        host_id="urn:uuid:00000000-0000-4000-8000-000000000469",
        agent_name="jeb-operation-qualification",
        agent_version="1.0.0",
    )
    journal_summary = validate_journal(journal)
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    binding = FileProvenanceBinding(
        path="notes/note.txt",
        bytes=source.stat().st_size,
        sha256=source_sha256,
        status="captured",
        journal_id=journal_summary.journal_id,
        current_state_id=journal_summary.current_state_id,
    )
    provenance_sha256 = hashlib.sha256(
        build_portable_provenance_set(
            bindings=(binding,),
            journals={journal_summary.journal_id: journal},
        )
    ).hexdigest()
    ingress_headers = {
        "Authorization": basic_authorization(
            "qualification",
            "qualification-password-2",
        )
    }

    public_operation_ids: list[str] = []

    def public_ingress_handler(request: httpx.Request) -> httpx.Response:
        started = time.perf_counter()
        operation_id = ""
        if request.method == "POST" and request.url.path == "/files/":
            operation_id = "create_tusd_ingress_upload"
            response = httpx.Response(
                201,
                headers={"Location": "/files/00000000000040008000000000000469"},
            )
        elif request.method == "PUT" and "/journals/" in request.url.path:
            operation_id = "put_public_tus_ingress_provenance_journal"
            response = httpx.Response(200, json={"status": "accepted"})
        elif request.method == "PUT" and request.url.path.endswith("/binding"):
            operation_id = "put_public_tus_ingress_provenance_binding"
            response = httpx.Response(200, json={"status": "accepted"})
        elif request.method == "HEAD" and request.url.path.startswith("/files/"):
            operation_id = "head_tusd_ingress_upload"
            response = httpx.Response(200, headers={"Upload-Offset": "0"})
        elif request.method == "PATCH" and request.url.path.startswith("/files/"):
            operation_id = "patch_tusd_ingress_upload"
            response = httpx.Response(
                204,
                headers={"Upload-Offset": str(source.stat().st_size)},
            )
        elif request.method == "GET" and request.url.path.startswith("/publications/"):
            operation_id = "get_tus_ingress_publication"
            response = httpx.Response(
                200,
                json={
                    "format": "jeb-ingress-publication/v1",
                    "status": "accepted",
                    "upload_id": "00000000000040008000000000000469",
                    "path": binding.path,
                    "bytes": binding.bytes,
                    "payload_sha256": binding.sha256,
                    "provenance_identity": provenance_sha256,
                },
            )
        else:
            return httpx.Response(404)
        observer.record_external_server(
            operation_id,
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )
        public_operation_ids.append(operation_id)
        return response

    ingress_client = JebIngressClient(
        source="qualification",
        password="qualification-password-2",
        base_url="https://jeb.example.test",
        transport=httpx.MockTransport(public_ingress_handler),
    )
    public_started = time.perf_counter()
    try:
        public_result = ingress_client.upload_file(
            source,
            relative_path=binding.path,
            binding={
                "path": binding.path,
                "bytes": binding.bytes,
                "sha256": binding.sha256,
                "status": binding.status,
                "journal_id": binding.journal_id,
                "current_state_id": binding.current_state_id,
            },
            journals={journal_summary.journal_id: journal},
        )
    finally:
        ingress_client.close()
    assert public_result["status"] == "accepted"
    public_elapsed_ms = (time.perf_counter() - public_started) * 1000
    for operation_id in public_operation_ids:
        observer.record_external_client(operation_id, elapsed_ms=public_elapsed_ms)

    assert transport.get("/internal/ingress/tus/auth", headers=ingress_headers).status_code == 204
    hook = transport.post(
        "/internal/ingress/tus/hooks",
        json={
            "Type": "pre-create",
            "Event": {
                "Upload": {
                    "Size": binding.bytes,
                    "Offset": 0,
                    "MetaData": {
                        "filename": binding.path,
                        "sha256": binding.sha256,
                        "provenance_sha256": provenance_sha256,
                    },
                }
            },
        },
        headers=ingress_headers,
    )
    assert hook.status_code == 200
    upload_id = hook.json()["ChangeFileInfo"]["ID"]
    journal_response = transport.put(
        (f"/internal/ingress/tus/provenance/{upload_id}/journals/{journal_summary.journal_id}"),
        content=journal,
        headers={
            **ingress_headers,
            "Content-Type": "application/json-seq",
            "X-Riverhog-Provenance-SHA256": journal_summary.journal_sha256,
        },
    )
    assert journal_response.status_code == 200
    binding_response = transport.put(
        f"/internal/ingress/tus/provenance/{upload_id}/binding",
        json={
            "path": binding.path,
            "bytes": binding.bytes,
            "sha256": binding.sha256,
            "status": binding.status,
            "journal_id": binding.journal_id,
            "current_state_id": binding.current_state_id,
        },
        headers=ingress_headers,
    )
    assert binding_response.status_code == 200
    services.store.reject_ingress_publication(
        upload_id,
        code="qualification_complete",
        message="qualification fixture closed its synthetic publication",
    )
    events = client.list_lifecycle_events(limit=100)
    assert events.events
    restarted_services = create_services(config_from_env(env))
    upgrade_state(restarted_services.config)
    restarted_transport = TestClient(
        create_app(JebServiceState(services=restarted_services)),
        headers=API_HEADERS,
    )
    restarted_client = JebApiClient(
        "http://testserver",
        token="test-jeb-management-token",
        allow_insecure_http=True,
    )
    restarted_client._client = TimeoutNeutralTestClient(  # type: ignore[assignment]
        restarted_transport
    )
    resumed_events = restarted_client.list_lifecycle_events(
        after=events.next_cursor,
        limit=100,
    )
    assert resumed_events.events == []
    assert resumed_events.next_cursor == events.next_cursor

    plan = client.plan_source_removal("qualification", purge=True)
    removed = client.remove_source("qualification", challenge=str(plan["challenge"]))
    assert removed["status"] == "removed"

    expected = {
        str(operation["operationId"])
        for path in application.openapi()["paths"].values()
        for method, operation in path.items()
        if method in {"get", "post", "put", "patch", "delete"}
    }
    expected.update(
        route.operation_id or route.name
        for route in application.routes
        if isinstance(route, APIRoute) and route.path.startswith(("/v1/", "/internal/", "/health/"))
    )
    expected.update(public_operation_ids)
    observer.require(expected)


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
        assert json.loads(denied.value.read().decode("utf-8")) == {
            "error": {
                "code": "unauthorized",
                "message": "valid Jeb bearer credentials are required",
            }
        }
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
    upgrade_state(services.config)
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

        missing_status, missing = request_json("GET", f"{base}/missing")
        assert missing_status == 404
        assert missing == {
            "error": {
                "code": "not_found",
                "message": "source not found: missing",
            }
        }

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

        payload_sha256 = hashlib.sha256(b"notes").hexdigest()
        binding = {
            "path": "notes/note.txt",
            "bytes": 5,
            "sha256": payload_sha256,
            "status": "omitted",
            "omission_reason": "fixture explicitly omits source observation",
        }
        provenance_sha256 = hashlib.sha256(
            build_portable_provenance_set(
                bindings=(
                    FileProvenanceBinding(
                        path="notes/note.txt",
                        bytes=5,
                        sha256=payload_sha256,
                        status="omitted",
                        omission_reason="fixture explicitly omits source observation",
                    ),
                ),
                journals={},
            )
        ).hexdigest()
        status, prepared = post_json(
            f"http://{host}:{port}/internal/ingress/tus/hooks",
            {
                "Type": "pre-create",
                "Event": {
                    "Upload": {
                        "Size": 5,
                        "Offset": 0,
                        "MetaData": {
                            "filename": "notes/note.txt",
                            "sha256": payload_sha256,
                            "provenance_sha256": provenance_sha256,
                        },
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
        assert metadata["jeb_payload_sha256"] == payload_sha256
        assert metadata["jeb_provenance_sha256"] == provenance_sha256

        put_ingress_binding(
            services.config.ingress,
            upload_id=upload_id,
            source_id="phone",
            payload=binding,
        )

        staging = services.config.ingress.tus_staging_dir
        staging.mkdir(parents=True, exist_ok=True)
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


def test_jeb_tus_pre_create_returns_bounded_public_rejections(tmp_path: Path) -> None:
    services = services_for(jeb_env(tmp_path))
    server = start_jeb_service_server("127.0.0.1", 0, JebServiceState(services=services))
    try:
        host, port = server.server_address[:2]
        endpoint = f"http://{host}:{port}/internal/ingress/tus/hooks"
        request = {
            "Type": "pre-create",
            "Event": {
                "Upload": {
                    "Size": 5,
                    "Offset": 0,
                    "MetaData": {"filename": "../note.txt"},
                }
            },
        }

        assert post_json(
            endpoint,
            request,
            headers={"Authorization": basic_authorization("phone", "wrong-password")},
        ) == (
            200,
            {
                "RejectUpload": True,
                "HTTPResponse": {
                    "StatusCode": 401,
                    "Body": "invalid Jeb ingress credentials",
                    "Header": {"Content-Type": "text/plain"},
                },
            },
        )
        assert post_json(
            endpoint,
            request,
            headers={"Authorization": basic_authorization("phone", "phone-password")},
        ) == (
            200,
            {
                "RejectUpload": True,
                "HTTPResponse": {
                    "StatusCode": 400,
                    "Body": "invalid Jeb TUS upload",
                    "Header": {"Content-Type": "text/plain"},
                },
            },
        )
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


def test_jeb_service_operations_survive_service_recreation(tmp_path: Path) -> None:
    env = jeb_env(tmp_path)
    services = create_services(config_from_env(env))
    upgrade_state(services.config)
    operation = services.operations.start(operation="once", run=lambda: None)
    operation_id = str(operation["id"])
    deadline = time.monotonic() + 5
    while services.operations.get(operation_id)["state"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.01)

    restarted = create_services(config_from_env(env))
    upgrade_state(restarted.config)

    shown = restarted.operations.get(operation_id)
    page = restarted.operations.list_page(
        page=1,
        per_page=25,
        sort="started_at",
        order="desc",
        query="once",
        state="succeeded",
        all_items=False,
    )
    assert shown["state"] == "succeeded"
    assert shown["completed_at"]
    assert page["total"] == 1
    assert page["operations"] == [shown]


def test_jeb_service_startup_marks_an_interrupted_operation_failed(tmp_path: Path) -> None:
    env = jeb_env(tmp_path)
    services = create_services(config_from_env(env))
    upgrade_state(services.config)
    services.runtime.initialize()
    services.store.create_service_operation(
        operation_id="interrupted-operation",
        operation="archive-now",
        started_at="2026-08-01T12:00:00Z",
        source="phone",
        attempt_id="attempt-1",
    )

    restarted = create_services(config_from_env(env))
    upgrade_state(restarted.config)
    server = start_jeb_service_server("127.0.0.1", 0, JebServiceState(services=restarted))
    try:
        shown = restarted.operations.get("interrupted-operation")
        assert shown["state"] == "failed"
        assert shown["completed_at"]
        assert shown["failure"] == "service restarted before operation completed"
        assert restarted.operations.active_summary() is None
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


def test_jeb_running_app_publishes_its_openapi_contract(tmp_path: Path) -> None:
    services = services_for(jeb_env(tmp_path))
    server = start_jeb_service_server("127.0.0.1", 0, JebServiceState(services=services))
    try:
        host, port = server.server_address[:2]
        document = read_json(f"http://{host}:{port}/openapi.json", headers={})

        assert document["info"] == {
            "title": "Jeb API",
            "description": (
                "Source enrollment, watched-drop ingestion, and target delivery management."
            ),
            "version": importlib.metadata.version("jeb-server"),
        }
        assert document["components"]["securitySchemes"]["JebBearer"] == {
            "type": "http",
            "description": "Jeb management API bearer token.",
            "scheme": "bearer",
        }
        public_operations = [
            operation
            for path, methods in document["paths"].items()
            if path.startswith("/v1/")
            for method, operation in methods.items()
            if method in {"get", "post", "patch", "delete"}
        ]
        operation_ids = [operation["operationId"] for operation in public_operations]
        assert operation_ids
        assert len(operation_ids) == len(set(operation_ids))
        assert all(operation["security"] == [{"JebBearer": []}] for operation in public_operations)
        assert all(
            {status for status in operation["responses"] if status.isdigit() and int(status) >= 400}
            == {"400", "401", "403", "404", "409", "500", "503"}
            for operation in public_operations
        )
        assert all(
            operation["responses"]["400"]["content"]["application/json"]["schema"]
            == {"$ref": "#/components/schemas/ErrorResponse"}
            for operation in public_operations
        )
        assert {
            "ArchiveNowIn",
            "AttemptOut",
            "AttemptPageOut",
            "ErrorResponse",
            "SourceCreateIn",
            "SourceOut",
            "SourcePageOut",
            "SourceUpdateIn",
        } <= set(document["components"]["schemas"])

        direct = create_app(JebServiceState(services=services)).openapi()
        assert direct["paths"] == document["paths"]
    finally:
        server.shutdown()
        server.server_close()

from __future__ import annotations

import errno
import hashlib
import importlib
import json
import logging
import os
import sqlite3
import sys
import tarfile
import threading
from pathlib import Path
from types import ModuleType

import pytest
from fastapi.testclient import TestClient
from munchy_target_support.metadata_projection import project_immich_metadata
from munchy_target_support.uvicorn_logging import DropHealthAccessLogFilter
from riverhog_api_client import Conflict, NotFound
from riverhog_provenance import create_observation_journal, validate_journal
from time_formats import utc_timestamp_now

SERVER_MODULES = {
    "external": "munchy_core.adapters.external",
    "riverhog": "munchy_core.adapters.riverhog",
    "persistence": "munchy_core.persistence",
    "domain_errors": "munchy_core.domain.errors",
    "template_domain": "munchy_core.domain.job_templates",
    "domain_models": "munchy_core.domain.models",
    "application_keys": "munchy_core.persistence.application_keys",
    "lifecycle_store": "munchy_core.persistence.lifecycle_events",
    "state_store": "munchy_core.persistence.sqlite_state",
    "template_registry": "munchy_core.persistence.template_registry",
    "handoff_port": "munchy_core.ports.handoff",
    "command_runtime": "munchy_core.runtime.commands",
    "runtime_config": "munchy_core.runtime.config",
    "execution_runtime": "munchy_core.runtime.execution",
    "admission_service": "munchy_core.services.admission",
    "cleanup_service": "munchy_core.services.cleanup",
    "diagnostic_service": "munchy_core.services.diagnostics",
    "event_service": "munchy_core.services.events",
    "handoff_service": "munchy_core.services.handoffs",
    "job_service": "munchy_core.services.jobs",
    "media_service": "munchy_core.services.media",
    "processing_service": "munchy_core.services.processing",
    "retention_service": "munchy_core.services.retention",
    "routing_service": "munchy_core.services.routing",
    "scheduling_service": "munchy_core.services.scheduling",
    "template_service": "munchy_core.services.templates",
    "upload_service": "munchy_core.services.uploads",
}


def omitted_test_provenance() -> dict[str, str]:
    return {"status": "omitted", "omission_reason": "test fixture"}


def load_server(tmp_path: Path, monkeypatch) -> ModuleType:  # type: ignore[no-untyped-def]
    if "MUNCHY_APPLICATION_AUTH_REQUIRED" not in os.environ:
        monkeypatch.setenv("MUNCHY_APPLICATION_AUTH_REQUIRED", "0")
    monkeypatch.setenv("MUNCHY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MUNCHY_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("MUNCHY_TUSD_DIR", str(tmp_path / "tusd"))
    monkeypatch.setenv("MUNCHY_GPU_RUNTIME_DIR", str(tmp_path / "gpu-runtime"))
    for module_name in tuple(sys.modules):
        if module_name != "munchy_api.app" and not module_name.startswith("munchy_core."):
            continue
        sys.modules.pop(module_name, None)
        package_name, _, attribute = module_name.rpartition(".")
        package = sys.modules.get(package_name)
        if package is not None and hasattr(package, attribute):
            delattr(package, attribute)
    server = importlib.import_module("munchy_api.app")
    for alias, module_name in SERVER_MODULES.items():
        setattr(server, alias, importlib.import_module(module_name))
    server.persistence.initialize_persistence()
    return server


def _request_with_principal(
    server: ModuleType,
    *,
    app: str,
    key_id: str,
):  # type: ignore[no-untyped-def]
    request = server.Request({"type": "http", "state": {}})
    request.state.principal = server.MunchyPrincipal(
        app=app,
        key_id=key_id,
        permissions=frozenset({"*"}),
    )
    return request


def test_health_access_log_filter_drops_only_health_paths() -> None:
    access_filter = DropHealthAccessLogFilter()
    health = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", "/health/ready", "1.1", 200),
        None,
    )
    api = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", "/v1/jobs/job-1", "1.1", 200),
        None,
    )

    assert access_filter.filter(health) is False
    assert access_filter.filter(api) is True


def test_external_submission_identity_maps_to_confined_provenance_path(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    journal_id = "urn:uuid:00000000-0000-4000-8000-000000000042"

    upload_root = server.upload_service.shared_input_upload_root("../../outside")
    journal_path = server.upload_service.input_provenance_journal_path("../../outside", journal_id)

    configured_root = (server.runtime_config.GPU_RUNTIME_DIR / "input-uploads").resolve()
    assert upload_root.parent == configured_root
    assert journal_path.parent == upload_root / ".riverhog" / "provenance" / "journals"
    assert journal_path.name == f"{journal_id}.json-seq"


def test_munchy_state_commands_report_and_verify_the_current_revision(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    server = load_server(tmp_path, monkeypatch)

    assert server.main(["state", "status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert server.main(["state", "upgrade", "--json"]) == 0
    upgraded = json.loads(capsys.readouterr().out)
    assert server.main(["state", "verify", "--json"]) == 0
    verified = json.loads(capsys.readouterr().out)

    assert status["condition"] == "current"
    assert upgraded["current_revision"] == "v1_0001"
    assert verified["condition"] == "current"


def test_api_auth_protects_v1_but_not_health(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MUNCHY_APPLICATION_AUTH_REQUIRED", "1")
    server = load_server(tmp_path, monkeypatch)
    server.persistence.initialize_persistence()
    server_token = server.state_store.application_keys().create(
        app="desktop-client",
        permissions=["submissions:manage"],
    )["token"]

    client = TestClient(server.app)

    assert client.get("/health/live").status_code == 200
    unauthorized = client.get("/v1/jobs")
    assert unauthorized.status_code == 401
    assert unauthorized.headers["WWW-Authenticate"] == "Bearer"
    authorized = client.get(
        "/v1/jobs",
        headers={"Authorization": f"Bearer {server_token}"},
    )
    assert authorized.status_code == 200
    assert authorized.json()["jobs"] == []


def test_readiness_requires_a_current_job_template_registry(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    with TestClient(server.app) as client:
        ready = client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json() == {"service": "munchy", "status": "ok"}

        definition = {
            "schema_version": 1,
            "kind": "munchy.job",
            "job": {"handoff": {"destination": "unsupported"}},
        }
        with sqlite3.connect(server.runtime_config.STATE_DB_PATH) as conn:
            conn.execute(
                """
                INSERT INTO job_templates(
                    template_id, definition, resolved_job, digest, revision, enabled,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "invalid-example",
                    json.dumps(definition, sort_keys=True),
                    "{}",
                    server.template_domain.job_template_digest(definition),
                    1,
                    1,
                    "2026-07-18T00:00:00.000001Z",
                    "2026-07-18T00:00:00.000001Z",
                ),
            )
            conn.commit()

        not_ready = client.get("/health/ready")
        assert not_ready.status_code == 503
        assert not_ready.json() == {
            "error": {
                "code": "service_unavailable",
                "message": "job template registry does not satisfy the current contract",
            }
        }


def job_template_definition(*, archive_store: str = "b2") -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "munchy.job",
        "job": {
            "workflow_mode": "collection_archive",
            "handoff": {
                "destination": "riverhog",
                "options": {
                    "archive_store": archive_store,
                },
            },
        },
    }


def test_admin_auth_is_separate_from_submission_auth(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MUNCHY_APPLICATION_AUTH_REQUIRED", "1")
    monkeypatch.setenv("MUNCHY_ADMIN_TOKEN", "admin-token")
    server = load_server(tmp_path, monkeypatch)
    server.persistence.initialize_persistence()
    submission_token = server.state_store.application_keys().create(
        app="desktop-client",
        permissions=["submissions:manage"],
    )["token"]

    with TestClient(server.app) as client:
        assert (
            client.get(
                "/v1/admin/job-templates",
                headers={"Authorization": f"Bearer {submission_token}"},
            ).status_code
            == 401
        )
        assert (
            client.get(
                "/v1/jobs",
                headers={"Authorization": "Bearer admin-token"},
            ).status_code
            == 401
        )
        assert (
            client.get(
                "/v1/admin/job-templates",
                headers={"Authorization": "Bearer admin-token"},
            ).status_code
            == 200
        )


def test_munchy_event_stream_is_scoped_to_authenticated_application(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MUNCHY_APPLICATION_AUTH_REQUIRED", "1")
    monkeypatch.setenv("MUNCHY_ADMIN_TOKEN", "admin-token")
    server = load_server(tmp_path, monkeypatch)

    with TestClient(server.app) as client:
        desktop_created = client.post(
            "/v1/admin/apps/desktop-client/keys",
            headers={"Authorization": "Bearer admin-token"},
            json={"permissions": ["events:read"]},
        )
        jeb_created = client.post(
            "/v1/admin/apps/jeb/keys",
            headers={"Authorization": "Bearer admin-token"},
            json={"permissions": ["events:read"]},
        )
        operator_created = client.post(
            "/v1/admin/apps/operator/keys",
            headers={"Authorization": "Bearer admin-token"},
            json={"permissions": ["events:read_all"]},
        )
        assert desktop_created.status_code == 201
        assert jeb_created.status_code == 201
        assert operator_created.status_code == 201

        server.lifecycle_store.lifecycle_event_log().append(
            server.cloud_event(
                source="urn:munchy",
                type="io.riverhog.munchy.job.received",
                subject="desktop-job",
                data={},
            ),
            owner="desktop-client",
        )
        server.lifecycle_store.lifecycle_event_log().append(
            server.cloud_event(
                source="urn:munchy",
                type="io.riverhog.munchy.job.received",
                subject="jeb-job",
                data={},
            ),
            owner="jeb",
        )

        desktop = client.get(
            "/v1/events",
            headers={"Authorization": f"Bearer {desktop_created.json()['token']}"},
        )
        jeb = client.get(
            "/v1/events",
            headers={"Authorization": f"Bearer {jeb_created.json()['token']}"},
        )
        operator = client.get(
            "/v1/events",
            headers={"Authorization": f"Bearer {operator_created.json()['token']}"},
        )

    assert [event["subject"] for event in desktop.json()["events"]] == ["desktop-job"]
    assert [event["subject"] for event in jeb.json()["events"]] == ["jeb-job"]
    assert [event["subject"] for event in operator.json()["events"]] == [
        "desktop-job",
        "jeb-job",
    ]


def test_job_template_crud_is_revision_guarded_and_database_listed(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)

    with TestClient(server.app) as client:
        created_response = client.post(
            "/v1/admin/job-templates",
            json={
                "template_id": "camera-archive",
                "definition": job_template_definition(),
            },
        )
        assert created_response.status_code == 201
        created = created_response.json()
        assert created["template_id"] == "camera-archive"
        assert created["revision"] == 1
        assert created["enabled"] is True
        assert created["resolved_job"]["workflow_mode"] == "collection_archive"

        assert (
            client.post(
                "/v1/admin/job-templates",
                json={
                    "template_id": "phone-archive",
                    "definition": job_template_definition(),
                },
            ).status_code
            == 201
        )

        all_templates = client.get(
            "/v1/admin/job-templates",
            params={"all": "true", "per_page": 1},
        ).json()
        assert all_templates["page"] == 1
        assert all_templates["pages"] == 1
        assert all_templates["per_page"] == 2
        assert [item["template_id"] for item in all_templates["templates"]] == [
            "camera-archive",
            "phone-archive",
        ]

        listed = client.get(
            "/v1/admin/job-templates",
            params={"q": "camera", "sort": "revision", "order": "desc"},
        ).json()
        assert listed["total"] == 1
        assert [item["template_id"] for item in listed["templates"]] == ["camera-archive"]
        assert "definition" not in listed["templates"][0]

        replaced_response = client.put(
            "/v1/admin/job-templates/camera-archive",
            json={
                "definition": job_template_definition(archive_store="deep"),
                "enabled": True,
                "expected_revision": 1,
            },
        )
        assert replaced_response.status_code == 200
        replaced = replaced_response.json()
        assert replaced["revision"] == 2
        assert replaced["digest"] != created["digest"]

        conflict = client.put(
            "/v1/admin/job-templates/camera-archive",
            json={
                "definition": job_template_definition(archive_store="deep"),
                "enabled": True,
                "expected_revision": 1,
            },
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"] == {
            "code": "job_template_revision_conflict",
            "message": "job template revision conflict",
            "details": {
                "template_id": "camera-archive",
                "expected_revision": 1,
                "current_revision": 2,
            },
        }

        disabled = client.post(
            "/v1/admin/job-templates/camera-archive/disable",
            json={"expected_revision": 2},
        ).json()
        assert disabled["enabled"] is False
        assert disabled["revision"] == 3

        removed = client.delete(
            "/v1/admin/job-templates/camera-archive",
            params={"expected_revision": 3},
        )
        assert removed.status_code == 200
        assert removed.json() == {"template_id": "camera-archive", "state": "removed"}
        assert client.get("/v1/admin/job-templates/camera-archive").status_code == 404


def test_job_template_rejects_submission_owned_fields(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    definition = job_template_definition()
    definition["job"]["run_id"] = "fixed"  # type: ignore[index]

    with TestClient(server.app) as client:
        response = client.post(
            "/v1/admin/job-templates",
            json={"template_id": "invalid", "definition": definition},
        )

    assert response.status_code == 400
    assert "submission-owned" in response.json()["error"]["message"]


def test_riverhog_handoff_template_owns_tags(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    monkeypatch.setattr(server.job_service, "schedule_pending_jobs", lambda *_args, **_kwargs: [])
    definition = job_template_definition()
    definition["job"]["handoff"]["options"]["tags"] = ["camera"]  # type: ignore[index]

    with TestClient(server.app) as client:
        created = client.post(
            "/v1/admin/job-templates",
            json={"template_id": "camera-archive", "definition": definition},
        )
        submitted = client.post(
            "/v1/submissions",
            json={
                "submission_id": "submission-1",
                "template_id": "camera-archive",
                "run_id": "20260101T000000.123456Z",
                "files": [
                    {
                        "path": "video/a.mp4",
                        "bytes": 4,
                        "sha256": "a" * 64,
                        "provenance": omitted_test_provenance(),
                    }
                ],
            },
        )

    assert created.status_code == 201, created.json()
    assert submitted.status_code == 202, submitted.json()
    assert server.state_store.load_job("submission-1")["handoff"]["options"]["tags"] == ["camera"]


def test_job_template_accepts_named_sidecar_rules(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    definition = job_template_definition()
    definition["job"]["routing"] = {  # type: ignore[index]
        "routes": [
            {
                "id": "video",
                "group": "video",
                "when": {"path": {"suffix": ".mp4"}},
            }
        ],
        "sidecars": {
            "metadata": {
                "path": "{parent}/{stem}.xml",
                "primary": {"path": {"suffix": ".mp4"}},
                "sidecar": {"path": {"suffix": ".xml"}},
            }
        },
    }
    definition["groups"] = {"video": {"output_mode": "video", "tasks": ["archive_video"]}}

    with TestClient(server.app) as client:
        response = client.post(
            "/v1/admin/job-templates",
            json={"template_id": "camera-archive", "definition": definition},
        )
        created = response.json()
        replaced = client.put(
            "/v1/admin/job-templates/camera-archive",
            json={
                "definition": created["definition"],
                "enabled": True,
                "expected_revision": created["revision"],
            },
        )

    assert response.status_code == 201
    assert created["definition"]["job"]["routing"]["sidecars"] == {
        "metadata": definition["job"]["routing"]["sidecars"]["metadata"]  # type: ignore[index]
    }
    assert created["resolved_job"]["routing"]["sidecars"][0]["id"] == "metadata"
    assert replaced.status_code == 200
    assert replaced.json()["revision"] == 2


def test_submission_resolves_declared_template_input(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    monkeypatch.setattr(server.job_service, "schedule_pending_jobs", lambda *_args, **_kwargs: [])
    definition = {
        "schema_version": 1,
        "kind": "munchy.job",
        "inputs": {
            "route": {
                "required": True,
                "enum": ["camera-main", "camera-telephoto"],
            }
        },
        "job": {
            "workflow_mode": "review",
            "output_mode": "video",
            "tasks": ["qcut_video"],
            "handoff": {
                "destination": "rclone",
                "options": {
                    "location": "reviews/{route_id}/{profile_id}/{run_id}",
                },
            },
            "review": {
                "route_id": "{{route}}",
                "profile_id": "webm-q42",
            },
        },
        "groups": {"review": {"output_mode": "video", "tasks": ["qcut_video"]}},
    }

    with TestClient(server.app) as client:
        assert (
            client.post(
                "/v1/admin/job-templates",
                json={"template_id": "camera-review", "definition": definition},
            ).status_code
            == 201
        )
        response = client.post(
            "/v1/submissions",
            json={
                "submission_id": "submission-1",
                "template_id": "camera-review",
                "inputs": {"route": "camera-telephoto"},
                "run_id": "20260101T000000.123456Z",
                "files": [
                    {
                        "path": "review/a.mp4",
                        "bytes": 4,
                        "sha256": "a" * 64,
                        "provenance": omitted_test_provenance(),
                    }
                ],
            },
        )

    assert response.status_code == 202, response.json()
    assert response.json()["inputs"] == {"route": "camera-telephoto"}
    assert server.state_store.load_job("submission-1")["review"]["route_id"] == "camera-telephoto"


def test_submission_preflight_resolves_template_without_creating_state(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)

    with TestClient(server.app) as client:
        assert (
            client.post(
                "/v1/admin/job-templates",
                json={
                    "template_id": "camera-archive",
                    "definition": job_template_definition(),
                },
            ).status_code
            == 201
        )
        response = client.post(
            "/v1/submissions/preflight",
            json={
                "template_id": "camera-archive",
                "run_id": "20260101T000000.123456Z",
                "files": [
                    {
                        "path": "video/a.mp4",
                        "bytes": 4,
                        "sha256": "a" * 64,
                        "provenance": omitted_test_provenance(),
                    }
                ],
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["accepted"] is True
    assert payload["template_id"] == "camera-archive"
    assert payload["template_revision"] == 1
    assert payload["content_inspection"] == "after_upload"
    assert payload["files_total"] == 1


def test_submission_creation_is_idempotent_and_snapshots_template_revision(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    monkeypatch.setattr(server.job_service, "schedule_pending_jobs", lambda *_args, **_kwargs: [])
    request = {
        "submission_id": "submission-1",
        "template_id": "camera-archive",
        "run_id": "20260101T000000.123456Z",
        "files": [
            {
                "path": "video/a.mp4",
                "bytes": 4,
                "sha256": "a" * 64,
                "provenance": omitted_test_provenance(),
            }
        ],
    }

    with TestClient(server.app) as client:
        created_template = client.post(
            "/v1/admin/job-templates",
            json={
                "template_id": "camera-archive",
                "definition": job_template_definition(),
            },
        ).json()
        created_response = client.post("/v1/submissions", json=request)
        assert created_response.status_code == 202
        created = created_response.json()
        assert created["submission_id"] == "submission-1"
        assert created["template_id"] == "camera-archive"
        assert created["template_revision"] == 1
        assert created["template_digest"] == created_template["digest"]
        assert created["upload"]["files_total"] == 1

        replaced = client.put(
            "/v1/admin/job-templates/camera-archive",
            json={
                "definition": job_template_definition(archive_store="deep"),
                "enabled": True,
                "expected_revision": 1,
            },
        )
        assert replaced.status_code == 200

        retried = client.post("/v1/submissions", json=request)
        assert retried.status_code == 202
        assert retried.json()["template_revision"] == 1

        conflict_request = dict(request)
        conflict_request["files"] = [
            {
                "path": "video/b.mp4",
                "bytes": 4,
                "sha256": "b" * 64,
                "provenance": omitted_test_provenance(),
            }
        ]
        conflict = client.post("/v1/submissions", json=conflict_request)
        assert conflict.status_code == 409
        assert conflict.json()["error"] == {
            "code": "submission_conflict",
            "message": "submission conflict",
            "details": {"submission_id": "submission-1"},
        }

    job = server.state_store.load_job("submission-1")
    assert job["template_id"] == "camera-archive"
    assert job["template_revision"] == 1
    assert job["handoff"]["options"] == {"archive_store": "b2"}


def test_submission_file_upload_is_scoped_to_registered_manifest(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    monkeypatch.setattr(server.job_service, "schedule_pending_jobs", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(server.upload_service, "head_tusd_upload", lambda _url: -1)
    monkeypatch.setattr(
        server.upload_service, "create_tusd_upload", lambda _path, _length: "http://tusd/files/1"
    )

    with TestClient(server.app) as client:
        client.post(
            "/v1/admin/job-templates",
            json={
                "template_id": "camera-archive",
                "definition": job_template_definition(),
            },
        )
        client.post(
            "/v1/submissions",
            json={
                "submission_id": "submission-1",
                "template_id": "camera-archive",
                "run_id": "20260101T000000.123456Z",
                "files": [
                    {
                        "path": "video/a.mp4",
                        "bytes": 4,
                        "sha256": "a" * 64,
                        "provenance": omitted_test_provenance(),
                    }
                ],
            },
        )
        upload = client.post("/v1/submissions/submission-1/files/video/a.mp4/upload")
        unknown = client.post("/v1/submissions/submission-1/files/video/b.mp4/upload")

    assert upload.status_code == 201
    assert upload.json()["protocol"] == "tus"
    assert upload.json()["length"] == 4
    assert unknown.status_code == 404


def test_handoff_failure_policy_is_runtime_job_option(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    monkeypatch.setattr(server.riverhog, "RIVERHOG_HANDOFF_ENABLED", True)
    data_path = server.upload_service.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"x")
    server.upload_service.save_input_upload(
        {
            "input_upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "storage_hint": {
                "workflow_mode": "collection_archive",
                "handoff_destination": "riverhog",
                "output_mode": "video",
                "tasks": ["archive_video"],
                "groups": {},
            },
            "files": [
                {
                    "path": "video/a.mp4",
                    "bytes": 1,
                    "sha256": hashlib.sha256(b"x").hexdigest(),
                    "file_upload_id": "upload-a",
                    "provenance": omitted_test_provenance(),
                }
            ],
        }
    )

    job = server.job_service.create_job_state_from_request(
        server.domain_models.CreateJobRequest(
            input_upload_id="upload-1",
            run_id="20260101T000000Z",
            tasks=["archive_video"],
            handoff={
                "destination": "riverhog",
                "options": {},
                "on_failure": "cancel",
            },
        )
    )

    assert job["handoff"] == {
        "destination": "riverhog",
        "options": {},
        "on_failure": "cancel",
        "safe_to_delete": False,
        "state": "pending",
    }


def test_input_file_upload_response_preserves_the_public_tusd_location(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)

    response = server.job_service.input_file_upload_response(
        upload_url="http://server.test/files/.munchy-server%2Fuploads%2Fabc",
        offset=3,
        length=8,
        status={"upload_state": "partial"},
    )

    assert response == {
        "protocol": "tus",
        "upload_url": "http://server.test/files/.munchy-server%2Fuploads%2Fabc",
        "offset": 3,
        "length": 8,
        "checksum_algorithm": "sha256",
        "headers": {"Tus-Resumable": "1.0.0"},
        "file": {"upload_state": "partial"},
    }


def test_structured_input_upload_accepts_source_prefixed_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)

    req = server.domain_models.CreateInputUploadRequest(
        files=[
            server.domain_models.InputFileSpec(
                path="front-door/telephoto/clip.mp4",
                bytes=12,
                sha256="a" * 64,
                provenance=omitted_test_provenance(),
            )
        ],
        storage_hint=server.domain_models.InputUploadStorageHint(
            workflow_mode="collection_archive",
            handoff_destination="riverhog",
            structured_routing=True,
            groups={
                "video": server.domain_models.StorageGroupHint(
                    output_mode="video",
                    tasks=["archive_video"],
                )
            },
        ),
    )

    assert req.files[0].path == "front-door/telephoto/clip.mp4"
    assert (
        server.upload_service.upload_file_resolved_group(
            {"path": "front-door/telephoto/clip.mp4", "structured_routing": True}
        )
        == ""
    )


def test_preserve_groups_are_copy_only(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)

    group = server.domain_models.GroupConfig(
        output_mode="preserve",
        tasks=["archive_video"],
    )
    storage_group = server.domain_models.StorageGroupHint(
        output_mode="preserve",
        tasks=["archive_video"],
    )

    assert group.output_mode == "preserve"
    assert group.tasks == []
    assert storage_group.output_mode == "preserve"
    assert storage_group.tasks == []


def test_review_job_request_accepts_clip_plan_config(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)

    req = server.domain_models.CreateJobRequest(
        workflow_mode="review",
        tasks=["qcut_video"],
        handoff={
            "destination": "rclone",
            "options": {"location": "review-remote:reviews"},
        },
        review={
            "route_id": "camera-main-video",
            "profile_id": "webm-q42",
            "clip_plan": {"target_seconds": 90},
        },
    )

    assert req.review is not None
    assert req.review.clip_plan is not None
    assert req.review.clip_plan.target_seconds == 90
    assert req.review.clip_plan.min_seconds == 6
    assert req.review.clip_plan.max_seconds == 9

    with pytest.raises(ValueError, match="min_seconds"):
        server.domain_models.CreateJobRequest(
            workflow_mode="review",
            tasks=["qcut_video"],
            handoff={
                "destination": "rclone",
                "options": {"location": "review-remote:reviews"},
            },
            review={
                "route_id": "camera-main-video",
                "profile_id": "webm-q42",
                "clip_plan": {"min_seconds": 10, "max_seconds": 9},
            },
        )


def test_review_job_request_accepts_general_sweep_config(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)

    req = server.domain_models.CreateJobRequest(
        workflow_mode="review",
        handoff={
            "destination": "rclone",
            "options": {
                "location": "review-remote:reviews/{route_id}/{profile_id}",
            },
        },
        groups={
            "video": {"output_mode": "video", "tasks": ["qcut_video"]},
            "photo": {"output_mode": "preserve", "tasks": []},
        },
        review={
            "sweep": {
                "axes": {
                    "archive.max_height": [720, 1080],
                    "archive.audio.bitrate": ["64k", "96k"],
                },
                "variants": [
                    {
                        "profile_id": "q40",
                        "encode_settings": {"archive": {"quality": 40}},
                    }
                ],
            },
        },
    )

    assert req.review is not None
    assert req.review.route_id is None
    assert req.review.profile_id is None
    assert req.review.sweep is not None
    assert req.review.sweep.axes == {
        "archive.max_height": [720, 1080],
        "archive.audio.bitrate": ["64k", "96k"],
    }


def test_review_job_storage_hint_uses_handoff_destination(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)

    req = server.domain_models.CreateJobRequest(
        workflow_mode="review",
        output_mode="video",
        tasks=["qcut_video"],
        handoff={
            "destination": "rclone",
            "options": {"location": "review-remote:reviews"},
        },
        groups={
            "camera-main-video": {
                "output_mode": "video",
                "tasks": ["qcut_video"],
                "max_parallel_encodes": 4,
                "eager_pipeline_batches": 1,
            }
        },
        review={
            "route_id": "camera-main-video",
            "profile_id": "webm-q42",
        },
    )

    assert req.groups["camera-main-video"].max_parallel_encodes == 4
    assert req.groups["camera-main-video"].eager_pipeline_batches == 1
    hint = server.admission_service.storage_hint_for_job_request(req).model_dump(exclude_none=True)

    assert hint == {
        "workflow_mode": "review",
        "handoff_destination": "rclone",
        "output_mode": "video",
        "tasks": ["qcut_video"],
        "groups": {
            "camera-main-video": {
                "output_mode": "video",
                "tasks": ["qcut_video"],
                "eager_pipeline_batches": 1,
            }
        },
        "structured_routing": False,
    }


def test_create_job_request_accepts_full_metadata_projection_config(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)

    req = server.domain_models.CreateJobRequest(
        workflow_mode="collection_archive",
        handoff={"destination": "riverhog"},
        groups={
            "phone-video": {
                "output_mode": "video",
                "tasks": ["archive_video"],
                "metadata_projection": {
                    "creators": ["Example Operator", "Example Collaborator", "Example Operator"],
                    "device": {
                        "make": " Apple ",
                        "model": " iPhone SE (2nd generation) ",
                    },
                    "gps": {
                        "latitude": 48.999527523960296,
                        "longitude": -122.74040765142755,
                    },
                    "tags": ["device/example-phone", "device/example-phone"],
                    "include_context_tags": False,
                },
            }
        },
    )

    projection = server.upload_service.group_dump(req.groups["phone-video"])["metadata_projection"]

    assert projection["creators"] == ["Example Operator", "Example Collaborator"]
    assert projection["device"] == {
        "make": "Apple",
        "model": "iPhone SE (2nd generation)",
    }
    assert projection["gps"] == {
        "latitude": 48.999527523960296,
        "longitude": -122.74040765142755,
    }
    assert projection["tags"] == ["device/example-phone"]
    assert projection["include_context_tags"] is False


def test_create_job_request_accepts_disabled_group_metadata_projection(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)

    req = server.domain_models.CreateJobRequest(
        workflow_mode="collection_archive",
        handoff={"destination": "riverhog"},
        groups={
            "device-state": {
                "output_mode": "preserve",
                "metadata_projection": False,
            },
        },
    )

    assert (
        server.upload_service.group_dump(req.groups["device-state"])["metadata_projection"] is False
    )


def test_create_job_request_accepts_routing_extra_exiftool_tags(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)

    req = server.domain_models.CreateJobRequest(
        workflow_mode="collection_archive",
        handoff={"destination": "riverhog"},
        groups={
            "video": {
                "output_mode": "video",
                "tasks": ["archive_video"],
            },
        },
        routing={
            "extra_exiftool_tags": [
                "AndroidCaptureFPS",
                "AndroidMake",
                "AndroidCaptureFPS",
            ],
            "routes": [
                {
                    "id": "camera-video",
                    "group": "video",
                    "when": {"path": {"suffix": ".mp4"}},
                }
            ],
        },
    )

    routing = req.routing.model_dump(exclude_none=True)

    assert routing["extra_exiftool_tags"] == ["AndroidCaptureFPS", "AndroidMake"]


def test_create_job_request_accepts_sidecar_fact_extractors(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)

    req = server.domain_models.CreateJobRequest(
        workflow_mode="collection_archive",
        handoff={"destination": "riverhog"},
        groups={
            "video": {
                "output_mode": "video",
                "tasks": ["archive_video"],
            },
        },
        routing={
            "sidecars": [
                {
                    "id": "camera_xml",
                    "format": "xml",
                    "path": "{parent}/{stem}M01.XML",
                    "primary": {"path": {"suffix": ".mp4"}},
                    "facts": {
                        "source": "exiftool",
                        "tags": ["VendorGroup", "VendorFieldName", "VendorFieldValue"],
                        "extractors": [
                            {
                                "type": "name_value",
                                "name_tag": "VendorFieldName",
                                "value_tag": "VendorFieldValue",
                                "requires": [{"tag": "VendorGroup", "contains": "Gps"}],
                                "fields": {"Latitude": "exif.gps_latitude"},
                            }
                        ],
                    },
                }
            ],
            "routes": [
                {
                    "id": "sidecar-camera-video",
                    "group": "video",
                    "when": {
                        "fact": "sidecars.camera_xml.facts.exif.gps_latitude",
                        "exists": True,
                    },
                }
            ],
        },
    )

    routing = req.routing.model_dump(exclude_none=True)

    assert routing["sidecars"][0]["facts"]["extractors"] == [
        {
            "type": "name_value",
            "name_tag": "VendorFieldName",
            "value_tag": "VendorFieldValue",
            "requires": [{"tag": "VendorGroup", "contains": "Gps"}],
            "fields": {"Latitude": "exif.gps_latitude"},
        }
    ]


def test_completed_structured_file_routes_by_path_without_full_upload(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    shared_file = server.upload_service.shared_input_upload_root("upload-1") / "front-door/clip.mp4"
    shared_file.parent.mkdir(parents=True, exist_ok=True)
    shared_file.write_bytes(b"video")
    upload = server.upload_service.save_input_upload_raw(
        {
            "input_upload_id": "upload-1",
            "state": "uploading",
            "storage_hint": {
                "workflow_mode": "collection_archive",
                "handoff_destination": "riverhog",
                "structured_routing": True,
                "groups": {
                    "video": {"output_mode": "video", "tasks": ["archive_video"]},
                    "preserve": {"output_mode": "preserve", "tasks": []},
                },
            },
            "files": [
                {
                    "path": "front-door/clip.mp4",
                    "bytes": 5,
                    "file_upload_id": "front-door-clip",
                    "input_upload_id": "upload-1",
                    "structured_routing": True,
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                },
                {
                    "path": "front-door/pending.mp4",
                    "bytes": 5,
                    "file_upload_id": "front-door-pending",
                    "input_upload_id": "upload-1",
                    "structured_routing": True,
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                },
            ],
        }
    )
    job = {
        "job_id": "job-1",
        "input_upload_id": "upload-1",
        "routing": {
            "routes": [
                {
                    "id": "front-door-video",
                    "group": "video",
                    "when": {"path": {"prefix": "front-door", "suffix": ".mp4"}},
                }
            ]
        },
    }
    groups = {
        "video": {"output_mode": "video", "tasks": ["archive_video"]},
        "preserve": {"output_mode": "preserve", "tasks": []},
    }

    routed = server.routing_service.route_completed_input_files(job, upload, groups)
    video_files = server.upload_service.upload_files_for_groups(routed, {"video"})

    assert [item["path"] for item in video_files] == ["front-door/clip.mp4"]
    assert video_files[0]["resolved_group"] == "video"
    assert video_files[0]["resolved_group_rel"] == "front-door/clip.mp4"
    assert server.upload_service.upload_file_group_rel_for_state(video_files[0], "video") == Path(
        "front-door/clip.mp4"
    )


def test_routing_manifest_records_actual_route_and_output(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    shared_file = server.upload_service.shared_input_upload_root("upload-1") / "phone/IMG_0001.MOV"
    shared_file.parent.mkdir(parents=True, exist_ok=True)
    shared_file.write_bytes(b"video")
    upload = server.upload_service.save_input_upload_raw(
        {
            "input_upload_id": "upload-1",
            "state": "uploading",
            "storage_hint": {
                "workflow_mode": "collection_archive",
                "handoff_destination": "riverhog",
                "structured_routing": True,
                "groups": {
                    "video": {"output_mode": "video", "tasks": ["archive_video"]},
                },
            },
            "files": [
                {
                    "path": "phone/IMG_0001.MOV",
                    "bytes": 5,
                    "sha256": "0" * 64,
                    "file_upload_id": "phone-img-1",
                    "input_upload_id": "upload-1",
                    "structured_routing": True,
                    "provenance": omitted_test_provenance(),
                }
            ],
        }
    )
    job = {
        "job_id": "job-1",
        "input_upload_id": "upload-1",
        "template_id": "phone-archive",
        "run_id": "20260628T000000Z",
        "routing": {
            "routes": [
                {
                    "id": "iphone-video",
                    "group": "video",
                    "into": "iphone/video",
                    "when": {"path": {"prefix": "phone", "suffix": ".mov"}},
                }
            ]
        },
    }
    groups = {
        "video": {
            "output_mode": "video",
            "tasks": ["archive_video"],
            "encode_profile": {"archive": {"container": "webm"}},
        }
    }

    routed = server.routing_service.route_completed_input_files(job, upload, groups)
    archive_dir = tmp_path / "archive"
    output = server.routing_service.archive_output_for_upload_file(
        server.upload_service.upload_files_for_groups(routed, {"video"})[0],
        group_name="video",
        group_config=groups["video"],
        archive_dir=archive_dir,
    )
    output.parent.mkdir(parents=True)
    output.write_bytes(b"webm")

    server.routing_service.write_routing_manifest(job, routed, groups, archive_dir)

    manifest = json.loads(
        (archive_dir / server.runtime_config.ROUTING_MANIFEST_FILENAME).read_text()
    )
    assert manifest["schema"] == "munchy_api_client.routing-manifest"
    assert manifest["job_id"] == "job-1"
    assert manifest["files"] == [
        {
            "output": {
                "bytes": 4,
                "exists": True,
                "path": "video/iphone/video/IMG_0001.webm",
            },
            "route": {
                "action": "upload",
                "group": "video",
                "group_rel_path": "iphone/video/IMG_0001.MOV",
                "id": "iphone-video",
            },
            "source": {
                "bytes": 5,
                "path": "phone/IMG_0001.MOV",
                "sha256": "0" * 64,
            },
        }
    ]


def test_routing_manifest_records_sidecar_evidence_without_fake_output(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    shared_root = server.upload_service.shared_input_upload_root("upload-1") / "phone"
    shared_root.mkdir(parents=True, exist_ok=True)
    (shared_root / "IMG_0001.MOV").write_bytes(b"video")
    (shared_root / "IMG_0001.MOV.xmp").write_text("<xmpmeta />", encoding="utf-8")
    upload = server.upload_service.save_input_upload_raw(
        {
            "input_upload_id": "upload-1",
            "state": "uploading",
            "storage_hint": {
                "workflow_mode": "collection_archive",
                "handoff_destination": "riverhog",
                "structured_routing": True,
                "groups": {
                    "video": {"output_mode": "video", "tasks": ["archive_video"]},
                },
            },
            "files": [
                {
                    "path": "phone/IMG_0001.MOV",
                    "bytes": 5,
                    "sha256": "0" * 64,
                    "file_upload_id": "phone-img-1",
                    "input_upload_id": "upload-1",
                    "structured_routing": True,
                    "provenance": omitted_test_provenance(),
                },
                {
                    "path": "phone/IMG_0001.MOV.xmp",
                    "bytes": 11,
                    "sha256": "1" * 64,
                    "file_upload_id": "phone-xmp-1",
                    "input_upload_id": "upload-1",
                    "structured_routing": True,
                    "provenance": omitted_test_provenance(),
                },
            ],
        }
    )
    job = {
        "job_id": "job-1",
        "input_upload_id": "upload-1",
        "template_id": "phone-archive",
        "run_id": "20260628T000000Z",
        "routing": {
            "sidecars": [
                {
                    "id": "xmp",
                    "format": "xmp",
                    "primary": {"path": {"suffix": ".mov"}},
                }
            ],
            "routes": [
                {
                    "id": "iphone-video",
                    "group": "video",
                    "into": "iphone/video",
                    "when": {"path": {"prefix": "phone", "suffix": ".mov"}},
                }
            ],
        },
    }
    groups = {
        "video": {
            "output_mode": "video",
            "tasks": ["archive_video"],
            "encode_profile": {"archive": {"container": "webm"}},
        }
    }
    routed = server.routing_service.route_completed_input_files(job, upload, groups)
    archive_dir = tmp_path / "archive"
    output = server.routing_service.archive_output_for_upload_file(
        server.upload_service.upload_files_for_groups(routed, {"video"})[0],
        group_name="video",
        group_config=groups["video"],
        archive_dir=archive_dir,
    )
    output.parent.mkdir(parents=True)
    output.write_bytes(b"webm")

    server.routing_service.write_routing_manifest(job, routed, groups, archive_dir)

    manifest = json.loads(
        (archive_dir / server.runtime_config.ROUTING_MANIFEST_FILENAME).read_text()
    )
    files_by_source = {item["source"]["path"]: item for item in manifest["files"]}
    evidence = files_by_source["phone/IMG_0001.MOV.xmp"]
    assert evidence["route"]["action"] == "evidence"
    assert evidence["route"]["sidecar"] == {
        "id": "xmp",
        "format": "xmp",
        "for": "phone/IMG_0001.MOV",
    }
    assert evidence["output"] == {
        "kind": "none",
        "reason": "sidecar_evidence",
    }
    assert evidence["source_artifact"] == {
        "kind": "source_artifact_sidecar",
        "primary_source": "phone/IMG_0001.MOV",
        "source_artifacts_path": "video/iphone/video/IMG_0001.webm.source-artifacts.tar.zst",
        "source_artifacts_entry": "sidecars/phone/IMG_0001.MOV.xmp",
    }


def test_metadata_projection_sidecars_written_for_archive_outputs(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    upload = server.upload_service.save_input_upload_raw(
        {
            "input_upload_id": "upload-1",
            "files": [
                {
                    "path": "video/IMG_0001.MOV",
                    "bytes": 5,
                    "file_upload_id": "phone-img-1",
                    "metadata_projection_metadata": {
                        "capture_date": "2026-06-28T20:30:40-07:00",
                        "capture_date_source": "exif.date_time_original",
                        "gps": {"latitude": 37.3317, "longitude": -122.0301},
                        "gps_source": "exif.gps_latitude+exif.gps_longitude",
                        "device": {
                            "make": "Apple",
                            "model": "iPhone SE (2nd generation)",
                        },
                        "creators": ["Example Operator"],
                        "tags": [],
                    },
                    "metadata_projection_captured_at": "2026-06-29T03:00:00Z",
                    "route_id": "iphone-video",
                    "resolved_group_rel": "iphone/video/IMG_0001.MOV",
                    "pair_kind": "live-photo",
                    "pair_role": "movie",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                }
            ],
        }
    )
    job = {
        "job_id": "job-1",
        "input_upload_id": "upload-1",
        "template_id": "phone-archive",
        "handoff": {"destination": "riverhog", "options": {}},
    }
    server.state_store.save_job(job)
    groups = {
        "video": {
            "output_mode": "video",
            "tasks": ["archive_video"],
            "encode_profile": {"archive": {"container": "webm"}},
            "metadata_projection": {
                "enabled": True,
                "device": {"make": "Apple", "model": "iPhone SE (2nd generation)"},
                "creators": ["Example Operator"],
                "tags": ["iphone-se2"],
            },
        }
    }
    archive_dir = tmp_path / "archive"
    output = server.routing_service.archive_output_for_upload_file(
        upload["files"][0],
        group_name="video",
        group_config=groups["video"],
        archive_dir=archive_dir,
    )
    output.parent.mkdir(parents=True)
    output.write_bytes(b"webm")

    updated = server.routing_service.write_metadata_projection_sidecars(
        job, upload, groups, archive_dir
    )

    sidecar = output.with_name("IMG_0001.webm.xmp")
    assert sidecar.exists()
    xmp = sidecar.read_text(encoding="utf-8")
    assert 'exif:DateTimeOriginal="2026-06-28T20:30:40-07:00"' in xmp
    assert 'xmp:MetadataDate="2026-06-29T03:00:00Z"' in xmp
    assert 'tiff:Make="Apple"' in xmp
    assert 'tiff:Model="iPhone SE (2nd generation)"' in xmp
    assert 'geo:lat="37.3317"' in xmp
    assert "<rdf:li>Example Operator</rdf:li>" in xmp
    assert "<rdf:li>iphone-se2</rdf:li>" in xmp
    assert "<rdf:li>munchy/template/phone-archive</rdf:li>" in xmp
    assert "<rdf:li>munchy/group/video</rdf:li>" in xmp
    assert "<rdf:li>munchy/route/iphone-video</rdf:li>" in xmp
    assert "<rdf:li>munchy/output/iphone/video</rdf:li>" in xmp
    assert "<rdf:li>munchy/pair/live-photo/movie</rdf:li>" in xmp
    assert updated["files"][0]["metadata_projection_sidecar"] == (
        "video/iphone/video/IMG_0001.webm.xmp"
    )
    assert server.state_store.load_job("job-1")["metadata_projection_result"]["sidecars"] == 1
    assert server.routing_service.routing_manifest_output_entry(output, archive_dir=archive_dir)[
        "metadata_sidecars"
    ] == [
        {
            "target": "immich_xmp",
            "path": "video/iphone/video/IMG_0001.webm.xmp",
            "bytes": sidecar.stat().st_size,
        }
    ]

    updated["files"][0]["metadata_projection_captured_at"] = "2026-07-01T00:00:00Z"
    server.upload_service.save_input_upload_raw(updated)
    job["handoff_adapter_state"] = {
        "state": "open",
        "files": {
            sidecar.relative_to(archive_dir).as_posix(): {
                "path": sidecar.relative_to(archive_dir).as_posix(),
                "bytes": sidecar.stat().st_size,
                "sha256": server.upload_service.file_sha256(sidecar),
                "uploaded_bytes": 0,
                "state": "uploading",
            }
        },
    }

    server.routing_service.write_metadata_projection_sidecars(job, updated, groups, archive_dir)

    assert sidecar.read_text(encoding="utf-8") == xmp


def test_metadata_projection_still_fails_for_missing_unhanded_output(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    upload = server.upload_service.save_input_upload_raw(
        {
            "input_upload_id": "upload-1",
            "files": [
                {
                    "path": "video/IMG_0001.MOV",
                    "bytes": 5,
                    "metadata_projection_metadata": {
                        "capture_date": "2026-06-28T20:30:40-07:00",
                        "capture_date_source": "exif.date_time_original",
                        "gps": {"latitude": 37.3317, "longitude": -122.0301},
                        "gps_source": "exif.gps_latitude+exif.gps_longitude",
                        "device": {"make": "Apple", "model": "iPhone"},
                        "creators": ["Example Operator"],
                    },
                    "resolved_group_rel": "iphone/video/IMG_0001.MOV",
                }
            ],
        }
    )
    job = {"job_id": "job-1", "input_upload_id": "upload-1"}
    groups = {
        "video": {
            "output_mode": "video",
            "tasks": ["archive_video"],
            "encode_profile": {"archive": {"container": "webm"}},
            "metadata_projection": {"enabled": True},
        }
    }

    with pytest.raises(RuntimeError, match="metadata projection output is missing"):
        server.routing_service.write_metadata_projection_sidecars(
            job, upload, groups, tmp_path / "archive"
        )


def test_metadata_projection_merges_existing_xmp_sidecar_for_preserve_outputs(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    server.upload_service.tusd_data_path("phone-xmp-1").parent.mkdir(parents=True, exist_ok=True)
    server.upload_service.tusd_data_path("phone-xmp-1").write_text(
        """<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/">
   <dc:subject><rdf:Bag><rdf:li>existing-tag</rdf:li></rdf:Bag></dc:subject>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
""",
        encoding="utf-8",
    )
    upload = server.upload_service.save_input_upload_raw(
        {
            "input_upload_id": "upload-1",
            "files": [
                {
                    "path": "phone/IMG_0001.HEIC",
                    "bytes": 5,
                    "file_upload_id": "phone-img-1",
                    "metadata_projection_metadata": {
                        "capture_date": "2026-06-28T20:30:40-07:00",
                        "capture_date_source": "exif.date_time_original",
                        "gps": {"latitude": 37.3317, "longitude": -122.0301},
                        "gps_source": "exif.gps_latitude+exif.gps_longitude",
                        "device": {
                            "make": "Apple",
                            "model": "iPhone SE (2nd generation)",
                        },
                        "creators": ["Example Operator"],
                        "tags": [],
                    },
                    "route_id": "iphone-photo",
                    "resolved_group": "photos",
                    "resolved_group_rel": "IMG_0001.HEIC",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                },
                {
                    "path": "phone/IMG_0001.HEIC.xmp",
                    "bytes": 20,
                    "file_upload_id": "phone-xmp-1",
                    "route_action": "evidence",
                    "sidecar_id": "xmp",
                    "sidecar_format": "xmp",
                    "sidecar_for": "phone/IMG_0001.HEIC",
                    "resolved_group": "photos",
                    "resolved_group_rel": "IMG_0001.HEIC.xmp",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                },
            ],
        }
    )
    job = {
        "job_id": "job-1",
        "input_upload_id": "upload-1",
        "template_id": "phone-archive",
    }
    server.state_store.save_job(job)
    groups = {
        "photos": {
            "output_mode": "preserve",
            "tasks": [],
            "metadata_projection": {
                "enabled": True,
                "device": {"make": "Apple", "model": "iPhone SE (2nd generation)"},
                "creators": ["Example Operator"],
            },
        }
    }
    archive_dir = tmp_path / "archive"
    output = archive_dir / "photos" / "IMG_0001.HEIC"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"heic")

    updated = server.routing_service.write_metadata_projection_sidecars(
        job, upload, groups, archive_dir
    )

    sidecar = output.with_name("IMG_0001.HEIC.xmp")
    assert sidecar.exists()
    xmp = sidecar.read_text(encoding="utf-8")
    assert "<rdf:li>existing-tag</rdf:li>" in xmp
    assert 'exif:DateTimeOriginal="2026-06-28T20:30:40-07:00"' in xmp
    assert 'tiff:Make="Apple"' in xmp
    assert "<rdf:li>munchy/template/phone-archive</rdf:li>" in xmp
    assert updated["files"][0]["metadata_projection_sidecar"] == "photos/IMG_0001.HEIC.xmp"
    assert "metadata_projection_sidecar" not in updated["files"][1]


def test_source_artifact_entries_include_sidecar_evidence_for_reencodes(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    materialized_group_root = tmp_path / "input" / "video"
    primary = materialized_group_root / "iphone" / "IMG_0001.MOV"
    sidecar = materialized_group_root / "iphone" / "IMG_0001.MOV.xmp"
    primary.parent.mkdir(parents=True)
    primary.write_bytes(b"mov")
    sidecar.write_text("<xmpmeta />", encoding="utf-8")
    primary_state = {
        "path": "phone/IMG_0001.MOV",
        "resolved_group": "video",
        "resolved_group_rel": "iphone/IMG_0001.MOV",
    }
    upload = {
        "files": [
            primary_state,
            {
                "path": "phone/IMG_0001.MOV.xmp",
                "route_action": "evidence",
                "sidecar_id": "xmp",
                "sidecar_format": "xmp",
                "sidecar_for": "phone/IMG_0001.MOV",
                "resolved_group": "video",
                "resolved_group_rel": "iphone/IMG_0001.MOV.xmp",
            },
        ]
    }

    entries = server.upload_service.source_artifacts_sidecar_entries(
        upload,
        [primary_state],
        group_name="video",
        materialized_group_root=materialized_group_root,
    )

    assert entries == {
        "iphone/IMG_0001.MOV": [
            {
                "id": "xmp",
                "format": "xmp",
                "path": str(sidecar),
                "arcname": "sidecars/iphone/IMG_0001.MOV.xmp",
                "source_rel_path": "phone/IMG_0001.MOV.xmp",
            }
        ]
    }


def test_metadata_projection_can_use_origin_provenance_timestamp(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    source = tmp_path / "REC_20260628_203040.WAV"
    source.write_bytes(b"wav")
    monkeypatch.setattr(
        server.routing_service, "ffprobe_for_routing", lambda _path: {"format": {}, "streams": []}
    )
    monkeypatch.setattr(server.routing_service, "exiftool_for_routing", lambda _path, **_kwargs: {})

    metadata = server.routing_service.projection_metadata_from_source(
        "voice/REC_20260628_203040.WAV",
        source,
        group_config={
            "metadata_projection": {
                "allow_missing_gps": True,
                "device": {"make": "eSonic", "model": "MEMOQ SR-600"},
                "creators": ["Example Operator"],
                "capture_date_sources": [
                    {"type": "embedded"},
                    {"type": "provenance_timestamp"},
                ],
            }
        },
        provenance={
            "status": "captured",
            "origin": {"timestamps": {"created": "2026-06-28T20:30:40+00:00"}},
        },
    )

    assert metadata.capture_date == "2026-06-28T20:30:40+00:00"
    assert metadata.capture_date_source == "provenance_timestamp:origin_created"


def test_metadata_projection_uses_declared_non_xmp_sidecar_facts(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.tusd_data_path("camera-video-1").parent.mkdir(parents=True, exist_ok=True)
    primary_path = server.upload_service.tusd_data_path("camera-video-1")
    sidecar_path = server.upload_service.tusd_data_path("camera-xml-1")
    primary_path.write_bytes(b"video")
    sidecar_path.write_text("<metadata />", encoding="utf-8")
    exiftool_calls: list[tuple[Path, tuple[str, ...]]] = []

    monkeypatch.setattr(
        server.routing_service,
        "ffprobe_for_routing",
        lambda _path: {"format": {}, "streams": []},
    )

    def fake_exiftool(path: Path, *, tags=()):  # type: ignore[no-untyped-def]
        exiftool_calls.append((path, tuple(tags)))
        if path == sidecar_path:
            assert tuple(tags) == (
                "VendorSidecarCreationDate",
                "VendorSidecarGroupName",
                "VendorSidecarFieldName",
                "VendorSidecarFieldValue",
            )
            return {
                "VendorSidecarCreationDate": "2026:07:08 15:46:25-07:00",
                "VendorSidecarGroupName": [
                    "GpsGroup",
                    "CameraUnitMetadataSet",
                ],
                "VendorSidecarFieldName": [
                    "VersionID",
                    "LatitudeRef",
                    "Latitude",
                    "LongitudeRef",
                    "Longitude",
                    "DateStamp",
                    "TimeStamp",
                    "Status",
                    "CaptureGammaEquation",
                ],
                "VendorSidecarFieldValue": [
                    "2.2.0.0",
                    "N",
                    "48;59;58.213",
                    "W",
                    "122;44;25.579",
                    "2026:07:08",
                    "22:46:21.000",
                    "A",
                    "rec709",
                ],
            }
        return {}

    monkeypatch.setattr(server.routing_service, "exiftool_for_routing", fake_exiftool)
    upload = {
        "input_upload_id": "upload-1",
        "files": [
            {
                "path": "camera/C0001.MP4",
                "bytes": 5,
                "file_upload_id": "camera-video-1",
                "resolved_group": "video",
                "resolved_group_rel": "camera/C0001.MP4",
                "provenance": omitted_test_provenance(),
                "sha256": "a" * 64,
            },
            {
                "path": "camera/C0001M01.XML",
                "bytes": 12,
                "file_upload_id": "camera-xml-1",
                "route_action": "evidence",
                "sidecar_id": "camera_xml",
                "sidecar_format": "xml",
                "sidecar_for": "camera/C0001.MP4",
                "resolved_group": "video",
                "resolved_group_rel": "camera/C0001M01.XML",
                "provenance": omitted_test_provenance(),
                "sha256": "a" * 64,
            },
        ],
    }
    job = {
        "job_id": "job-1",
        "routing": {
            "sidecars": [
                {
                    "id": "camera_xml",
                    "format": "xml",
                    "paths": ["{parent}/{stem}M01.XML"],
                    "primary": {"path": {"suffix": ".mp4"}},
                    "facts": {
                        "source": "exiftool",
                        "tags": [
                            "VendorSidecarCreationDate",
                            "VendorSidecarGroupName",
                            "VendorSidecarFieldName",
                            "VendorSidecarFieldValue",
                        ],
                        "extractors": [
                            {
                                "type": "name_value",
                                "name_tag": "VendorSidecarFieldName",
                                "value_tag": "VendorSidecarFieldValue",
                                "requires": [
                                    {
                                        "tag": "VendorSidecarGroupName",
                                        "contains": "GpsGroup",
                                    }
                                ],
                                "fields": {
                                    "Latitude": "exif.gps_latitude",
                                    "LatitudeRef": "exif.gps_latitude_ref",
                                    "Longitude": "exif.gps_longitude",
                                    "LongitudeRef": "exif.gps_longitude_ref",
                                },
                            }
                        ],
                    },
                }
            ],
            "routes": [
                {
                    "id": "camera-video",
                    "group": "video",
                    "when": {"path": {"suffix": ".mp4"}},
                }
            ],
        },
    }
    group_config = {
        "metadata_projection": {
            "allow_missing_gps": True,
            "device": {"make": "Example", "model": "Camera"},
            "creators": ["Example Operator"],
            "capture_date_sources": [
                {
                    "type": "sidecar",
                    "id": "camera_xml",
                    "fact": "exiftool.tags.vendor_sidecar_creation_date",
                },
            ],
            "gps_sources": [
                {"type": "sidecar", "id": "camera_xml"},
                {"type": "embedded"},
            ],
        }
    }

    changed = server.routing_service.ensure_file_projection_metadata(
        upload,
        upload["files"][0],
        job=job,
        group_config=group_config,
    )

    assert changed is True
    metadata = upload["files"][0]["metadata_projection_metadata"]
    assert metadata["capture_date"] == "2026-07-08T15:46:25-07:00"
    assert (
        metadata["capture_date_source"]
        == "sidecar:camera_xml:exiftool.tags.vendor_sidecar_creation_date"
    )
    assert metadata["gps"]["latitude"] == pytest.approx(48.99950361)
    assert metadata["gps"]["longitude"] == pytest.approx(-122.74043861)
    assert metadata["gps_source"] == "sidecar:camera_xml:exif.gps_latitude+exif.gps_longitude"
    assert (
        sidecar_path,
        (
            "VendorSidecarCreationDate",
            "VendorSidecarGroupName",
            "VendorSidecarFieldName",
            "VendorSidecarFieldValue",
        ),
    ) in exiftool_calls


def test_metadata_projection_can_use_configured_gps(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    source = tmp_path / "RecM0A_20260628_203040_203100_0_main.mp4"
    source.write_bytes(b"video")
    monkeypatch.setattr(
        server.routing_service, "ffprobe_for_routing", lambda _path: {"format": {}, "streams": []}
    )
    monkeypatch.setattr(
        server.routing_service,
        "exiftool_for_routing",
        lambda _path, **_kwargs: {"DateTimeOriginal": "2026:06:28 20:30:40Z"},
    )

    metadata = server.routing_service.projection_metadata_from_source(
        "camera/RecM0A_20260628_203040_203100_0_main.mp4",
        source,
        group_config={
            "metadata_projection": {
                "gps": {
                    "latitude": 48.999527523960296,
                    "longitude": -122.74040765142755,
                },
                "device": {"make": "Reolink", "model": "E1 Pro"},
                "creators": ["Example Operator"],
            }
        },
    )

    assert metadata.gps is not None
    assert metadata.gps.latitude == pytest.approx(48.999527523960296)
    assert metadata.gps.longitude == pytest.approx(-122.74040765142755)
    assert metadata.gps_source == "metadata_projection.gps"


def test_metadata_projection_path_regex_fallback_survives_exiftool_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    source = tmp_path / "Back Yard_00_20260630150631.mp4"
    source.write_bytes(b"video")
    monkeypatch.setattr(
        server.routing_service, "ffprobe_for_routing", lambda _path: {"format": {}, "streams": []}
    )

    def fail_exiftool(path: Path, **_kwargs: object) -> dict[str, object]:
        raise server.domain_errors.RoutingFailed(f"exiftool failed for {path.name}")

    monkeypatch.setattr(server.routing_service, "exiftool_for_routing", fail_exiftool)

    metadata = server.routing_service.projection_metadata_from_source(
        "back-yard-camera/Back Yard_00_20260630150631.mp4",
        source,
        group_config={
            "metadata_projection": {
                "capture_date_sources": [
                    {"type": "embedded"},
                    {
                        "type": "path_regex",
                        "name": "reolink_camera_filename",
                        "pattern": r"(?:^|/)[^/]+_[0-9]+_(?P<stamp>[0-9]{14})\.mp4$",
                        "datetime_group": "stamp",
                        "format": "%Y%m%d%H%M%S",
                        "timezone": "America/Los_Angeles",
                    },
                ],
                "gps": {
                    "latitude": 48.999527523960296,
                    "longitude": -122.74040765142755,
                },
                "device": {"make": "Reolink", "model": "Duo 3V PoE"},
                "creators": ["Example Operator"],
            }
        },
    )

    assert metadata.capture_date == "2026-06-30T15:06:31-07:00"
    assert metadata.capture_date_source == "path_regex:reolink_camera_filename"


def test_audio_container_metadata_args_write_capture_date_and_gps(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    metadata = project_immich_metadata(
        {
            "ffprobe.format_tags.creation_time": "2026-06-28T20:30:40-07:00",
            "ffprobe.format_tags.location": "+37.331700-122.030100+19.500/",
        },
        device_make="Apple",
        device_model="iPhone SE (2nd generation)",
        creators=["Example Operator"],
    )

    assert server.media_service.audio_container_metadata_args(metadata) == [
        "-metadata",
        "DATE=2026-06-28T20:30:40-07:00",
        "-metadata",
        "creation_time=2026-06-28T20:30:40-07:00",
        "-metadata",
        "ARTIST=Example Operator",
        "-metadata",
        "CREATOR=Example Operator",
        "-metadata",
        "MAKE=Apple",
        "-metadata",
        "MODEL=iPhone SE (2nd generation)",
        "-metadata",
        "LOCATION=+37.3317-122.0301+19.5/",
        "-metadata",
        "GPSLatitude=37.3317",
        "-metadata",
        "GPSLongitude=-122.0301",
        "-metadata",
        "GPSAltitude=19.5",
    ]


def test_gpu_payload_carries_required_projected_container_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    group_config = {
        "output_mode": "video",
        "tasks": ["archive_video"],
        "max_parallel_encodes": 3,
        "metadata_projection": {
            "device": {"make": "Apple", "model": "iPhone SE (2nd generation)"},
            "creators": ["Example Operator"],
        },
    }
    file_state = {
        "path": "camera/IMG_0001.MOV",
        "bytes": 5,
        "file_upload_id": "upload-a",
        "metadata_projection_metadata": {
            "capture_date": "2026-06-28T20:30:40-07:00",
            "capture_date_source": "exif.date_time_original",
            "gps": {"latitude": 37.3317, "longitude": -122.0301},
            "gps_source": "exif.gps_latitude+exif.gps_longitude",
            "device": {"make": "Apple", "model": "iPhone SE (2nd generation)"},
            "creators": ["Example Operator"],
            "tags": [],
        },
        "provenance": omitted_test_provenance(),
        "sha256": "a" * 64,
    }

    container_metadata, changed = server.routing_service.container_metadata_for_gpu_payload(
        {"job_id": "job-1"},
        {"input_upload_id": "upload-1", "files": [file_state]},
        [file_state],
        group_name="camera",
        group_config=group_config,
        tasks=["archive_video"],
    )
    payload = server.processing_service.build_eager_gpu_payload(
        {"job_id": "job-1", "template_id": "phone-archive"},
        batch_id="batch-1",
        group_name="camera",
        group_config=group_config,
        tasks=["archive_video"],
        container_metadata=container_metadata,
    )

    assert changed is False
    assert payload["max_parallel_encodes"] == 3
    assert payload["container_metadata_required"] is True
    assert payload["container_metadata"]["IMG_0001.MOV"]["device"] == {
        "make": "Apple",
        "model": "iPhone SE (2nd generation)",
    }
    assert payload["container_metadata"]["IMG_0001.MOV"]["creators"] == ["Example Operator"]


def test_eager_gpu_projection_reads_materialized_original_name(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    tusd_source = server.upload_service.tusd_data_path("upload-a")
    tusd_source.parent.mkdir(parents=True, exist_ok=True)
    tusd_source.write_bytes(b"video")
    upload = server.upload_service.save_input_upload_raw(
        {
            "input_upload_id": "upload-1",
            "storage_hint": {
                "workflow_mode": "collection_archive",
                "handoff_destination": "riverhog",
                "output_mode": "video",
                "tasks": ["archive_video"],
                "groups": {"camera": {"output_mode": "video", "tasks": ["archive_video"]}},
            },
            "files": [
                {
                    "path": "camera/Back Yard_00_20260630123456.mp4",
                    "bytes": 5,
                    "file_upload_id": "upload-a",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                }
            ],
        }
    )
    job = {
        "job_id": "job-1",
        "input_upload_id": "upload-1",
        "template_id": "camera-archive",
        "run_id": "20260101T000000Z",
    }
    server.state_store.save_job(job)
    group_config = {
        "output_mode": "video",
        "tasks": ["archive_video"],
        "metadata_projection": {
            "gps": {"latitude": 48.9995, "longitude": -122.7404},
            "device": {"make": "Reolink", "model": "Duo 3V PoE"},
            "creators": ["Example Operator"],
        },
    }
    payloads: list[dict[str, object]] = []
    exiftool_paths: list[Path] = []

    monkeypatch.setattr(
        server.routing_service, "ffprobe_for_routing", lambda _path: {"format": {}, "streams": []}
    )

    def fake_exiftool(path: Path, **_kwargs: object) -> dict[str, object]:
        exiftool_paths.append(path)
        if path == tusd_source:
            raise server.domain_errors.RoutingFailed(
                "extensionless tusd backing file was inspected"
            )
        return {"DateTimeOriginal": "2026:06:30 12:34:56-0700"}

    monkeypatch.setattr(server.routing_service, "exiftool_for_routing", fake_exiftool)
    monkeypatch.setattr(
        server.media_service, "start_gpu_job", lambda payload: payloads.append(payload)
    )

    updated = server.processing_service.start_eager_gpu_batch(
        job,
        upload,
        group_name="camera",
        group_config=group_config,
        file_states=[upload["files"][0]],
        archive_dir=tmp_path / "archive",
        space_checked=True,
    )

    assert len(exiftool_paths) == 1
    assert exiftool_paths[0] != tusd_source
    assert exiftool_paths[0].name == "Back Yard_00_20260630123456.mp4"
    assert "eager-input" in exiftool_paths[0].parts
    assert (
        payloads[0]["container_metadata"]["Back Yard_00_20260630123456.mp4"]["capture_date"]
        == "2026-06-30T12:34:56-07:00"
    )
    assert updated["files"][0]["metadata_projection_metadata"]["capture_date"] == (
        "2026-06-30T12:34:56-07:00"
    )


def test_eager_projection_and_upload_setup_mutations_are_serialized(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    upload_id = "upload-1"
    source_path = "camera/Back Yard_00_20260630123456.mp4"
    pending_path = "other/pending.mp4"
    tusd_source = server.upload_service.tusd_data_path("upload-a")
    tusd_source.parent.mkdir(parents=True, exist_ok=True)
    tusd_source.write_bytes(b"video")
    upload = server.upload_service.save_input_upload_raw(
        {
            "input_upload_id": upload_id,
            "storage_hint": {
                "workflow_mode": "collection_archive",
                "handoff_destination": "riverhog",
                "groups": {
                    "camera": {"output_mode": "video", "tasks": ["archive_video"]},
                    "other": {"output_mode": "preserve", "tasks": []},
                },
            },
            "files": [
                {
                    "path": source_path,
                    "bytes": 5,
                    "file_upload_id": "upload-a",
                    "target_path": server.upload_service.target_path_for(upload_id, source_path),
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                },
                {
                    "path": pending_path,
                    "bytes": 7,
                    "file_upload_id": "upload-b",
                    "target_path": server.upload_service.target_path_for(upload_id, pending_path),
                    "upload_url": None,
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                },
            ],
        }
    )
    job = {
        "job_id": "job-1",
        "input_upload_id": upload_id,
        "template_id": "camera-archive",
        "run_id": "20260101T000000Z",
    }
    server.state_store.save_job(job)
    group_config = {
        "output_mode": "video",
        "tasks": ["archive_video"],
        "encode_profile": {"archive": {"container": "webm"}},
        "metadata_projection": {
            "enabled": True,
            "device": {"make": "Example", "model": "Camera"},
            "gps": {"latitude": 48.9995, "longitude": -122.7404},
            "creators": ["Example Operator"],
        },
    }
    metadata_started = threading.Event()
    release_metadata = threading.Event()
    request_started = threading.Event()
    tusd_create_started = threading.Event()
    errors: list[BaseException] = []

    monkeypatch.setattr(
        server.routing_service, "ffprobe_for_routing", lambda _path: {"format": {}, "streams": []}
    )

    def fake_exiftool(_path: Path, **_kwargs: object) -> dict[str, object]:
        metadata_started.set()
        assert release_metadata.wait(timeout=5)
        return {"DateTimeOriginal": "2026:06:30 12:34:56-0700"}

    def fake_create_tusd_upload(target_path: str, _length: int) -> str:
        tusd_create_started.set()
        return f"{server.runtime_config.TUSD_PUBLIC_BASE_URL}/{target_path}"

    monkeypatch.setattr(server.routing_service, "exiftool_for_routing", fake_exiftool)
    monkeypatch.setattr(server.media_service, "start_gpu_job", lambda _payload: None)
    monkeypatch.setattr(server.upload_service, "create_tusd_upload", fake_create_tusd_upload)
    monkeypatch.setattr(server.upload_service, "head_tusd_upload", lambda _url: 0)

    def capture_metadata() -> None:
        try:
            server.processing_service.start_eager_gpu_batch(
                job,
                upload,
                group_name="camera",
                group_config=group_config,
                file_states=[upload["files"][0]],
                archive_dir=tmp_path / "archive",
                space_checked=True,
            )
        except BaseException as exc:  # pragma: no cover - re-raised by the parent thread
            errors.append(exc)

    def create_pending_upload() -> None:
        request_started.set()
        try:
            server.job_service._create_or_resume_input_file_upload(upload_id, pending_path)
        except BaseException as exc:  # pragma: no cover - re-raised by the parent thread
            errors.append(exc)

    metadata_thread = threading.Thread(target=capture_metadata)
    request_thread = threading.Thread(target=create_pending_upload)
    metadata_thread.start()
    assert metadata_started.wait(timeout=5)
    request_thread.start()
    assert request_started.wait(timeout=5)
    assert not tusd_create_started.wait(timeout=0.25)
    release_metadata.set()
    metadata_thread.join(timeout=5)
    request_thread.join(timeout=5)

    assert errors == []
    assert not metadata_thread.is_alive()
    assert not request_thread.is_alive()
    assert tusd_create_started.is_set()
    stored = server.upload_service.load_input_upload(upload_id)
    stored_by_path = {item["path"]: item for item in stored["files"]}
    assert stored_by_path[source_path]["metadata_projection_metadata"]["capture_date"] == (
        "2026-06-30T12:34:56-07:00"
    )
    assert stored_by_path[pending_path]["upload_url"]

    stored = server.processing_service.consume_input_upload_files(upload_id, {source_path})
    server.cleanup_service.shutil.rmtree(
        server.runtime_config.GPU_RUNTIME_DIR / "jobs" / "job-1" / "eager-input"
    )
    archive_dir = tmp_path / "archive"
    output = server.routing_service.archive_output_for_upload_file(
        stored_by_path[source_path],
        group_name="camera",
        group_config=group_config,
        archive_dir=archive_dir,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"webm")

    projected = server.routing_service.write_metadata_projection_sidecars(
        job,
        stored,
        {"camera": group_config},
        archive_dir,
    )

    projected_by_path = {item["path"]: item for item in projected["files"]}
    assert projected_by_path[source_path]["consumed_at"]
    assert output.with_name(f"{output.name}.xmp").exists()


def test_input_upload_state_compare_and_swap_rejects_stale_snapshots(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    server.upload_service.save_input_upload_raw(
        {
            "input_upload_id": "upload-1",
            "files": [
                {
                    "path": "camera/a.mp4",
                    "bytes": 5,
                    "file_upload_id": "upload-a",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                }
            ],
        }
    )
    stale = server.upload_service.load_input_upload_raw("upload-1")
    current = server.upload_service.load_input_upload_raw("upload-1")
    current["files"][0]["metadata_projection_metadata"] = {"capture_date": "2026-06-30"}
    server.upload_service.save_input_upload_raw(current)
    stale["files"][0]["upload_url"] = "https://uploads.example.invalid/upload-a"

    with pytest.raises(RuntimeError, match="stale input upload state"):
        server.upload_service.save_input_upload_raw(stale)

    stored = server.upload_service.load_input_upload_raw("upload-1")
    assert stored["files"][0]["metadata_projection_metadata"] == {"capture_date": "2026-06-30"}


def test_routed_structured_file_stays_complete_after_materialized_tusd_cleanup(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    upload_id = "upload-1"
    file_state = {
        "path": "phone/IMG_0001.HEIC",
        "bytes": 5,
        "file_upload_id": "phone-img-0001",
        "input_upload_id": upload_id,
        "structured_routing": True,
        "resolved_group": "phone-preserve",
        "resolved_group_rel": "iphone-se2/photo/rear-heic/IMG_0001.HEIC",
        "provenance": omitted_test_provenance(),
        "sha256": "a" * 64,
    }
    materialized = server.upload_service.shared_input_upload_root(upload_id) / (
        "phone-preserve/iphone-se2/photo/rear-heic/IMG_0001.HEIC"
    )
    materialized.parent.mkdir(parents=True, exist_ok=True)
    materialized.write_bytes(b"image")

    status = server.upload_service.upload_file_status(file_state)

    assert status["complete"] is True
    assert status["upload_state"] == "uploaded"
    assert status["uploaded_bytes"] == 5


def test_completed_structured_file_can_route_by_probe_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    shared_file = server.upload_service.shared_input_upload_root("upload-1") / "phone/IMG_0001.MOV"
    shared_file.parent.mkdir(parents=True, exist_ok=True)
    shared_file.write_bytes(b"video")
    upload = server.upload_service.save_input_upload_raw(
        {
            "input_upload_id": "upload-1",
            "state": "uploading",
            "storage_hint": {
                "workflow_mode": "collection_archive",
                "handoff_destination": "riverhog",
                "structured_routing": True,
                "groups": {"phone-video": {"output_mode": "video", "tasks": []}},
            },
            "files": [
                {
                    "path": "phone/IMG_0001.MOV",
                    "bytes": 5,
                    "file_upload_id": "phone-img-1",
                    "input_upload_id": "upload-1",
                    "structured_routing": True,
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                }
            ],
        }
    )

    def fake_ffprobe_for_routing(path: Path) -> dict[str, object]:
        assert path == shared_file
        return {
            "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "tags": {"make": "Apple"}},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "hevc",
                    "width": 3840,
                    "height": 2160,
                    "avg_frame_rate": "60000/1001",
                    "tags": {"handler_name": "Core Media Video"},
                }
            ],
        }

    monkeypatch.setattr(server.routing_service, "ffprobe_for_routing", fake_ffprobe_for_routing)
    job = {
        "job_id": "job-1",
        "input_upload_id": "upload-1",
        "routing": {
            "routes": [
                {
                    "id": "iphone-4k",
                    "group": "phone-video",
                    "when": {
                        "all": [
                            {"path": {"prefix": "phone", "suffix": ".mov"}},
                            {"fact": "video.long_edge", "min": 3000},
                            {"fact": "ffprobe.format_tags.make", "equals": "apple"},
                        ]
                    },
                }
            ]
        },
    }
    groups = {"phone-video": {"output_mode": "video", "tasks": []}}

    routed = server.routing_service.route_completed_input_files(job, upload, groups)

    assert (
        server.upload_service.upload_files_for_groups(routed, {"phone-video"})[0]["route_id"]
        == "iphone-4k"
    )


def test_sidecar_evidence_is_not_probed_during_runner_routing(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    video_file = server.upload_service.shared_input_upload_root("upload-1") / "phone/IMG_0001.MOV"
    sidecar_file = (
        server.upload_service.shared_input_upload_root("upload-1") / "phone/IMG_0001.MOV.xmp"
    )
    video_file.parent.mkdir(parents=True, exist_ok=True)
    video_file.write_bytes(b"video")
    sidecar_file.write_text("<xmpmeta />", encoding="utf-8")
    upload = server.upload_service.save_input_upload_raw(
        {
            "input_upload_id": "upload-1",
            "state": "uploading",
            "storage_hint": {
                "workflow_mode": "collection_archive",
                "handoff_destination": "riverhog",
                "structured_routing": True,
                "groups": {"phone-video": {"output_mode": "video", "tasks": []}},
            },
            "files": [
                {
                    "path": "phone/IMG_0001.MOV",
                    "bytes": 5,
                    "file_upload_id": "phone-img-1",
                    "input_upload_id": "upload-1",
                    "structured_routing": True,
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                },
                {
                    "path": "phone/IMG_0001.MOV.xmp",
                    "bytes": 11,
                    "file_upload_id": "phone-xmp-1",
                    "input_upload_id": "upload-1",
                    "structured_routing": True,
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                },
            ],
        }
    )

    probed: list[Path] = []

    def fake_ffprobe_for_routing(path: Path) -> dict[str, object]:
        probed.append(path)
        assert path == video_file
        return {
            "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "hevc",
                    "width": 3840,
                    "height": 2160,
                    "avg_frame_rate": "30/1",
                }
            ],
        }

    monkeypatch.setattr(server.routing_service, "ffprobe_for_routing", fake_ffprobe_for_routing)
    job = {
        "job_id": "job-1",
        "input_upload_id": "upload-1",
        "routing": {
            "sidecars": [
                {
                    "id": "xmp",
                    "format": "xmp",
                    "primary": {"path": {"suffix": ".mov"}},
                }
            ],
            "routes": [
                {
                    "id": "iphone-4k",
                    "group": "phone-video",
                    "when": {
                        "all": [
                            {"path": {"prefix": "phone", "suffix": ".mov"}},
                            {"fact": "video.long_edge", "min": 3000},
                        ]
                    },
                }
            ],
        },
    }
    groups = {"phone-video": {"output_mode": "video", "tasks": []}}

    routed = server.routing_service.route_completed_input_files(job, upload, groups)

    assert probed == [video_file]
    files_by_path = {item["path"]: item for item in routed["files"]}
    assert files_by_path["phone/IMG_0001.MOV"]["route_id"] == "iphone-4k"
    assert files_by_path["phone/IMG_0001.MOV.xmp"]["route_action"] == "evidence"
    assert files_by_path["phone/IMG_0001.MOV.xmp"]["sidecar_id"] == "xmp"
    assert files_by_path["phone/IMG_0001.MOV.xmp"]["sidecar_for"] == ("phone/IMG_0001.MOV")


def test_sidecar_routing_can_progress_by_complete_primary_sidecar_pair(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    shared_root = server.upload_service.shared_input_upload_root("upload-1") / "camera"
    shared_root.mkdir(parents=True, exist_ok=True)
    (shared_root / "C0001.MP4").write_bytes(b"video")
    (shared_root / "C0001M01.XML").write_text("<metadata />", encoding="utf-8")
    (shared_root / "C0002.MP4").write_bytes(b"video")
    upload = server.upload_service.save_input_upload_raw(
        {
            "input_upload_id": "upload-1",
            "state": "uploading",
            "storage_hint": {
                "workflow_mode": "collection_archive",
                "handoff_destination": "riverhog",
                "structured_routing": True,
                "groups": {"video": {"output_mode": "video", "tasks": ["archive_video"]}},
            },
            "files": [
                {
                    "path": "camera/C0001.MP4",
                    "bytes": 5,
                    "file_upload_id": "camera-video-1",
                    "input_upload_id": "upload-1",
                    "structured_routing": True,
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                },
                {
                    "path": "camera/C0001M01.XML",
                    "bytes": 12,
                    "file_upload_id": "camera-xml-1",
                    "input_upload_id": "upload-1",
                    "structured_routing": True,
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                },
                {
                    "path": "camera/C0002.MP4",
                    "bytes": 5,
                    "file_upload_id": "camera-video-2",
                    "input_upload_id": "upload-1",
                    "structured_routing": True,
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                },
                {
                    "path": "camera/C0002M01.XML",
                    "bytes": 12,
                    "file_upload_id": "camera-xml-2",
                    "input_upload_id": "upload-1",
                    "structured_routing": True,
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                },
            ],
        }
    )
    job = {
        "job_id": "job-1",
        "input_upload_id": "upload-1",
        "routing": {
            "sidecars": [
                {
                    "id": "camera_xml",
                    "format": "xml",
                    "path": "{parent}/{stem}M01.XML",
                    "primary": {"path": {"suffix": ".mp4"}},
                }
            ],
            "routes": [
                {
                    "id": "camera-video",
                    "group": "video",
                    "when": {"path": {"suffix": ".mp4"}},
                }
            ],
        },
    }
    groups = {
        "video": {
            "output_mode": "video",
            "tasks": ["archive_video"],
            "encode_profile": {"archive": {"container": "webm"}},
        }
    }

    routed = server.routing_service.route_completed_input_files(job, upload, groups)

    files_by_path = {item["path"]: item for item in routed["files"]}
    assert files_by_path["camera/C0001.MP4"]["route_id"] == "camera-video"
    assert files_by_path["camera/C0001M01.XML"]["route_action"] == "evidence"
    assert "route_id" not in files_by_path["camera/C0002.MP4"]
    assert "route_action" not in files_by_path["camera/C0002M01.XML"]

    ready = server.processing_service.ready_eager_files(
        job,
        routed,
        groups,
        {"video"},
        tmp_path / "archive",
        limit=32,
    )

    assert ready is not None
    group_name, file_states = ready
    assert group_name == "video"
    assert [item["path"] for item in file_states] == ["camera/C0001.MP4"]


def test_sidecar_facts_are_bounded_during_runner_routing(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    video_file = server.upload_service.shared_input_upload_root("upload-1") / "camera/C0001.MP4"
    sidecar_file = (
        server.upload_service.shared_input_upload_root("upload-1") / "camera/C0001M01.XML"
    )
    video_file.parent.mkdir(parents=True, exist_ok=True)
    video_file.write_bytes(b"video")
    sidecar_file.write_text("<metadata />", encoding="utf-8")
    upload = server.upload_service.save_input_upload_raw(
        {
            "input_upload_id": "upload-1",
            "state": "uploading",
            "storage_hint": {
                "workflow_mode": "collection_archive",
                "handoff_destination": "riverhog",
                "structured_routing": True,
                "groups": {"video": {"output_mode": "video", "tasks": []}},
            },
            "files": [
                {
                    "path": "camera/C0001.MP4",
                    "bytes": 5,
                    "file_upload_id": "camera-video-1",
                    "input_upload_id": "upload-1",
                    "structured_routing": True,
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                },
                {
                    "path": "camera/C0001M01.XML",
                    "bytes": 12,
                    "file_upload_id": "camera-xml-1",
                    "input_upload_id": "upload-1",
                    "structured_routing": True,
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                },
            ],
        }
    )
    exiftool_calls: list[tuple[Path, tuple[str, ...]]] = []

    def fail_ffprobe_for_routing(path: Path) -> dict[str, object]:
        raise AssertionError(f"ffprobe should not run for sidecar-facts-only routing: {path}")

    def fake_exiftool_for_routing(path: Path, *, tags=None):  # type: ignore[no-untyped-def]
        exiftool_calls.append((path, tuple(tags or ())))
        assert path == sidecar_file
        return {
            "EXIF:Make": "Example Imaging",
            "EXIF:Model": "Synthetic Camera",
        }

    monkeypatch.setattr(server.routing_service, "ffprobe_for_routing", fail_ffprobe_for_routing)
    monkeypatch.setattr(server.routing_service, "exiftool_for_routing", fake_exiftool_for_routing)
    job = {
        "job_id": "job-1",
        "input_upload_id": "upload-1",
        "routing": {
            "sidecars": [
                {
                    "id": "camera_xml",
                    "format": "xml",
                    "path": "{parent}/{stem}M01.XML",
                    "primary": {"path": {"suffix": ".mp4"}},
                    "facts": {"source": "exiftool", "tags": ["Make", "Model"]},
                }
            ],
            "routes": [
                {
                    "id": "sidecar-camera-video",
                    "group": "video",
                    "when": {
                        "all": [
                            {"path": {"suffix": ".mp4"}},
                            {
                                "fact": "sidecars.camera_xml.facts.exif.make",
                                "equals": "example imaging",
                            },
                        ]
                    },
                }
            ],
        },
    }

    routed = server.routing_service.route_completed_input_files(
        job,
        upload,
        {"video": {"output_mode": "video", "tasks": []}},
    )

    files_by_path = {item["path"]: item for item in routed["files"]}
    assert files_by_path["camera/C0001.MP4"]["route_id"] == "sidecar-camera-video"
    assert files_by_path["camera/C0001M01.XML"]["route_action"] == "evidence"
    assert files_by_path["camera/C0001M01.XML"]["sidecar_id"] == "camera_xml"
    assert exiftool_calls == [(sidecar_file, ("Make", "Model"))]


def test_sidecar_facts_unlock_primary_exiftool_during_runner_routing(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    video_file = server.upload_service.shared_input_upload_root("upload-1") / "camera/C0001.MP4"
    sidecar_file = (
        server.upload_service.shared_input_upload_root("upload-1") / "camera/C0001M01.XML"
    )
    video_file.parent.mkdir(parents=True, exist_ok=True)
    video_file.write_bytes(b"video")
    sidecar_file.write_text("<metadata />", encoding="utf-8")
    upload = server.upload_service.save_input_upload_raw(
        {
            "input_upload_id": "upload-1",
            "state": "uploading",
            "storage_hint": {
                "workflow_mode": "collection_archive",
                "handoff_destination": "riverhog",
                "structured_routing": True,
                "groups": {"video": {"output_mode": "video", "tasks": []}},
            },
            "files": [
                {
                    "path": "camera/C0001.MP4",
                    "bytes": 5,
                    "file_upload_id": "camera-video-1",
                    "input_upload_id": "upload-1",
                    "structured_routing": True,
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                },
                {
                    "path": "camera/C0001M01.XML",
                    "bytes": 12,
                    "file_upload_id": "camera-xml-1",
                    "input_upload_id": "upload-1",
                    "structured_routing": True,
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                },
            ],
        }
    )
    exiftool_calls: list[tuple[Path, tuple[str, ...]]] = []

    def fail_ffprobe_for_routing(path: Path) -> dict[str, object]:
        raise AssertionError(f"ffprobe should not run for exiftool-only routing: {path}")

    def fake_exiftool_for_routing(path: Path, *, tags=None):  # type: ignore[no-untyped-def]
        exiftool_calls.append((path, tuple(tags or ())))
        if path == sidecar_file:
            return {"EXIF:Make": "Example Imaging"}
        if path == video_file:
            assert "VideoAvgBitrate" in tuple(tags or ())
            return {"QuickTime:VideoAvgBitrate": "200 Mbps"}
        raise AssertionError(f"unexpected exiftool path: {path}")

    monkeypatch.setattr(server.routing_service, "ffprobe_for_routing", fail_ffprobe_for_routing)
    monkeypatch.setattr(server.routing_service, "exiftool_for_routing", fake_exiftool_for_routing)
    job = {
        "job_id": "job-1",
        "input_upload_id": "upload-1",
        "routing": {
            "extra_exiftool_tags": ["VideoAvgBitrate"],
            "sidecars": [
                {
                    "id": "camera_xml",
                    "format": "xml",
                    "path": "{parent}/{stem}M01.XML",
                    "primary": {"path": {"suffix": ".mp4"}},
                    "facts": {"source": "exiftool", "tags": ["Make"]},
                }
            ],
            "routes": [
                {
                    "id": "sidecar-and-bitrate-video",
                    "group": "video",
                    "when": {
                        "all": [
                            {"path": {"suffix": ".mp4"}},
                            {
                                "fact": "sidecars.camera_xml.facts.exif.make",
                                "equals": "example imaging",
                            },
                            {
                                "fact": "exiftool.tags.video_avg_bitrate",
                                "equals": "200 Mbps",
                            },
                        ]
                    },
                }
            ],
        },
    }

    routed = server.routing_service.route_completed_input_files(
        job,
        upload,
        {"video": {"output_mode": "video", "tasks": []}},
    )

    files_by_path = {item["path"]: item for item in routed["files"]}
    assert files_by_path["camera/C0001.MP4"]["route_id"] == ("sidecar-and-bitrate-video")
    assert files_by_path["camera/C0001M01.XML"]["route_action"] == "evidence"
    assert [path for path, _tags in exiftool_calls] == [sidecar_file, video_file]
    assert exiftool_calls[0] == (sidecar_file, ("Make",))


def test_routing_skips_expensive_tools_for_path_only_runner_route(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    state_file = server.upload_service.shared_input_upload_root("upload-1") / "camera/leinfo.sav"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_bytes(b"state")
    upload = server.upload_service.save_input_upload_raw(
        {
            "input_upload_id": "upload-1",
            "state": "uploading",
            "storage_hint": {
                "workflow_mode": "collection_archive",
                "handoff_destination": "riverhog",
                "structured_routing": True,
                "groups": {"state": {"output_mode": "preserve", "tasks": []}},
            },
            "files": [
                {
                    "path": "camera/leinfo.sav",
                    "bytes": 5,
                    "file_upload_id": "state-1",
                    "input_upload_id": "upload-1",
                    "structured_routing": True,
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                },
            ],
        }
    )

    def fail_ffprobe_for_routing(path: Path) -> dict[str, object]:
        raise AssertionError(f"unexpected ffprobe call for {path}")

    def fail_exiftool_for_routing(path: Path, *, tags=None):  # type: ignore[no-untyped-def]
        raise AssertionError(f"unexpected exiftool call for {path} with {tags}")

    monkeypatch.setattr(server.routing_service, "ffprobe_for_routing", fail_ffprobe_for_routing)
    monkeypatch.setattr(server.routing_service, "exiftool_for_routing", fail_exiftool_for_routing)
    job = {
        "job_id": "job-1",
        "input_upload_id": "upload-1",
        "routing": {
            "routes": [
                {
                    "id": "device-state",
                    "group": "state",
                    "when": {"path": {"filename_glob": "leinfo.sav"}},
                },
                {
                    "id": "camera-video",
                    "group": "video",
                    "when": {
                        "all": [
                            {"path": {"suffix": ".mp4"}},
                            {"fact": "video.codec", "equals": "hevc"},
                            {"fact": "exif.make", "equals": "example imaging"},
                        ]
                    },
                },
            ],
        },
    }

    routed = server.routing_service.route_completed_input_files(
        job,
        upload,
        {
            "state": {"output_mode": "preserve", "tasks": []},
            "video": {"output_mode": "video", "tasks": []},
        },
    )

    assert routed["files"][0]["route_id"] == "device-state"
    assert routed["files"][0]["resolved_group"] == "state"


def test_completed_structured_file_fails_when_no_route_matches(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    shared_file = server.upload_service.shared_input_upload_root("upload-1") / "unknown/clip.mp4"
    shared_file.parent.mkdir(parents=True, exist_ok=True)
    shared_file.write_bytes(b"video")
    upload = server.upload_service.save_input_upload_raw(
        {
            "input_upload_id": "upload-1",
            "state": "uploading",
            "storage_hint": {"workflow_mode": "collection_archive", "structured_routing": True},
            "files": [
                {
                    "path": "unknown/clip.mp4",
                    "bytes": 5,
                    "file_upload_id": "unknown-clip",
                    "input_upload_id": "upload-1",
                    "structured_routing": True,
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                }
            ],
        }
    )
    job = {
        "job_id": "job-1",
        "input_upload_id": "upload-1",
        "routing": {
            "routes": [
                {
                    "id": "front-door-video",
                    "group": "video",
                    "when": {"path": {"prefix": "front-door"}},
                }
            ]
        },
    }

    with pytest.raises(server.domain_errors.RoutingFailed, match="no matching route"):
        server.routing_service.route_completed_input_files(
            job,
            upload,
            {"video": {"output_mode": "video", "tasks": []}},
        )


def test_archive_admission_uses_eager_batch_peak_for_gpu_scratch(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    monkeypatch.setattr(server.runtime_config, "EAGER_ARCHIVE_BATCH_FILES", 2)
    monkeypatch.setattr(server.runtime_config, "EAGER_ARCHIVE_PIPELINE_BATCHES", 2)
    monkeypatch.setattr(server.runtime_config, "GPU_SCRATCH_MULTIPLIER", 2.5)
    monkeypatch.setattr(server.runtime_config, "EAGER_ARCHIVE_SCRATCH_MULTIPLIER", 0.5)
    monkeypatch.setattr(server.admission_service, "gpu_input_copy_multiplier", lambda: 0.0)
    files = [
        server.domain_models.InputFileSpec(
            path=f"camera/{index}.mp4",
            bytes=size,
            sha256=f"{index:064x}",
            provenance=omitted_test_provenance(),
        )
        for index, size in enumerate([100, 90, 80, 70, 60], start=1)
    ]
    hint = server.domain_models.InputUploadStorageHint(
        workflow_mode="collection_archive",
        handoff_destination="riverhog",
        groups={
            "camera": server.domain_models.StorageGroupHint(
                output_mode="video",
                tasks=["archive_video"],
            )
        },
    )

    required = server.admission_service.gpu_scratch_admission_required_bytes(files, hint)

    assert required == int((100 + 90 + 80 + 70) * 0.5)
    assert required < server.admission_service.gpu_scratch_required_bytes(
        sum(item.bytes for item in files), hint
    )


def test_audio_archive_hint_does_not_reserve_gpu_scratch(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    files = [
        server.domain_models.InputFileSpec(
            path="voice/REC_0001.wav",
            bytes=100,
            sha256="a" * 64,
            provenance=omitted_test_provenance(),
        )
    ]
    hint = server.domain_models.InputUploadStorageHint(
        workflow_mode="collection_archive",
        handoff_destination="riverhog",
        output_mode="audio",
        groups={"voice": server.domain_models.StorageGroupHint(output_mode="audio")},
    )

    assert hint.groups["voice"].tasks == ["archive_audio"]
    assert server.admission_service.storage_hint_has_gpu_work(hint) is False
    assert server.admission_service.gpu_scratch_required_bytes(100, hint) == 0
    assert server.admission_service.gpu_scratch_admission_required_bytes(files, hint) == 0


def test_eager_archive_audio_group_encodes_and_consumes_uploaded_files(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    upload_id = "upload-voice"
    source_data = server.upload_service.tusd_data_path("upload-a")
    source_data.parent.mkdir(parents=True, exist_ok=True)
    source_data.write_bytes(b"mp3")
    group_config = {
        "output_mode": "audio",
        "tasks": ["archive_audio"],
        "metadata_projection": {"enabled": False},
    }
    upload = server.upload_service.save_input_upload_raw(
        {
            "input_upload_id": upload_id,
            "files": [
                {
                    "path": "voice/R-00001_2606291200_REC.MP3",
                    "bytes": 3,
                    "file_upload_id": "upload-a",
                    "input_upload_id": upload_id,
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                }
            ],
        }
    )
    job = {
        "job_id": "job-voice",
        "input_upload_id": upload_id,
        "template_id": "voice-archive",
    }
    server.state_store.save_job(job)
    archive_dir = tmp_path / "archive"
    calls: list[tuple[Path, Path]] = []

    def fake_run_archive_audio_group(
        *,
        input_root: Path,
        output_root: Path,
        group_config: dict[str, object],
    ) -> dict[str, object]:
        calls.append((input_root, output_root))
        assert (input_root / "R-00001_2606291200_REC.MP3").read_bytes() == b"mp3"
        output_root.mkdir(parents=True, exist_ok=True)
        (output_root / "R-00001_2606291200_REC.opus").write_bytes(b"opus")
        return {"status": "succeeded", "count": 1, "items": []}

    monkeypatch.setattr(
        server.media_service, "run_archive_audio_group", fake_run_archive_audio_group
    )

    assert server.processing_service.eager_archive_group_names({"voice": group_config}) == {"voice"}
    updated = server.processing_service.start_eager_audio_batch(
        job,
        upload,
        group_name="voice",
        group_config=group_config,
        file_states=upload["files"],
        archive_dir=archive_dir,
    )

    stored_job = server.state_store.load_job("job-voice")
    stored_upload = server.upload_service.load_input_upload(upload_id)
    progress = server.processing_service.encode_progress_for_job(stored_job)
    assert calls
    assert (archive_dir / "voice" / "R-00001_2606291200_REC.opus").read_bytes() == b"opus"
    assert updated["files"][0]["consumed_at"]
    assert stored_upload["files"][0]["consumed_at"]
    assert not source_data.exists()
    assert progress is not None
    assert progress["mode"] == "eager_archive"
    assert progress["groups"] == ["voice"]
    assert progress["files_total"] == 1
    assert progress["files_encoded"] == 1
    assert progress["percent_files"] == 100.0


def test_archive_audio_group_encodes_opus_and_writes_source_artifacts(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    input_root = tmp_path / "input" / "voice"
    output_root = tmp_path / "archive" / "voice"
    source = input_root / "REC_20260628_203040.WAV"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"wav")
    group_config = {
        "output_mode": "audio",
        "tasks": ["archive_audio"],
        "encode_profile": {
            "target": "munchy-audio",
            "archive": {
                "codec": "opus",
                "container": "opus",
                "audio": {
                    "bitrate": "28k",
                    "sample_rate": 24000,
                    "channels": 1,
                    "vbr": True,
                    "application": "audio",
                    "compression_level": 10,
                    "frame_duration": 40,
                    "cutoff": 12000,
                },
            },
        },
        "metadata_projection": {
            "allow_missing_gps": True,
            "device": {"make": "eSonic", "model": "MEMOQ SR-600"},
            "creators": ["Example Operator"],
            "capture_date_sources": [
                {"type": "embedded"},
                {"type": "provenance_timestamp"},
            ],
        },
    }
    commands: list[list[str]] = []

    def fake_run_command(cmd: list[str], **_kwargs: object) -> dict[str, object]:
        commands.append(cmd)
        Path(cmd[-1]).write_bytes(b"opus")
        return {"command": cmd, "duration_s": 0.1}

    def fake_build_strict_source_artifacts(**kwargs: object) -> dict[str, object]:
        return {"path": str(kwargs["archive_mkv"]) + ".source-artifacts.tar.zst"}

    monkeypatch.setattr(server.command_runtime, "run_command", fake_run_command)
    monkeypatch.setattr(
        server.media_service, "build_strict_source_artifacts", fake_build_strict_source_artifacts
    )
    monkeypatch.setattr(
        server.routing_service, "ffprobe_for_routing", lambda _path: {"format": {}, "streams": []}
    )
    monkeypatch.setattr(server.routing_service, "exiftool_for_routing", lambda _path, **_kwargs: {})

    result = server.media_service.run_archive_audio_group(
        input_root=input_root,
        output_root=output_root,
        group_config=group_config,
        provenance_by_rel_path={
            source.name: {
                "status": "captured",
                "origin": {"timestamps": {"created": "2026-06-28T20:30:40+00:00"}},
            }
        },
    )

    output = output_root / "REC_20260628_203040.opus"
    assert result["status"] == "succeeded"
    assert result["count"] == 1
    assert output.read_bytes() == b"opus"
    assert commands == [
        [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-ignore_unknown",
            "-i",
            str(source),
            "-map",
            "0:a?",
            "-map_metadata",
            "0",
            "-vn",
            "-c:a",
            "libopus",
            "-ar",
            "24000",
            "-ac",
            "1",
            "-b:a",
            "28k",
            "-vbr",
            "on",
            "-compression_level",
            "10",
            "-application",
            "audio",
            "-frame_duration",
            "40",
            "-cutoff",
            "12000",
            "-metadata",
            "DATE=2026-06-28T20:30:40+00:00",
            "-metadata",
            "creation_time=2026-06-28T20:30:40+00:00",
            "-metadata",
            "ARTIST=Example Operator",
            "-metadata",
            "CREATOR=Example Operator",
            "-metadata",
            "MAKE=eSonic",
            "-metadata",
            "MODEL=MEMOQ SR-600",
            "-f",
            "opus",
            str(output),
        ]
    ]


def test_audio_archive_projection_uses_birthtime_and_conversion_only_source_artifacts(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    upload_id = "upload-voice"
    input_root = server.upload_service.shared_input_upload_root(upload_id) / "voice"
    output_root = tmp_path / "archive" / "voice"
    source = input_root / "memo" / "R-00013_2606222246_REC.MP3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"mp3")
    group_config = {
        "output_mode": "audio",
        "tasks": ["archive_audio"],
        "encode_profile": {
            "target": "munchy-audio",
            "archive": {
                "codec": "opus",
                "container": "opus",
                "audio": {"bitrate": "64k"},
            },
            "source": {"allow_conversion_only_container": True},
        },
        "metadata_projection": {
            "allow_missing_gps": True,
            "device": {"make": "eSonic", "model": "MEMOQ SR-600"},
            "creators": ["Example Operator"],
            "capture_date_sources": [
                {"type": "embedded"},
                {"type": "provenance_timestamp"},
            ],
            "tags": ["device/esonic-memoq-sr600-example"],
        },
    }

    def fake_run_command(cmd: list[str], **_kwargs: object) -> dict[str, object]:
        Path(cmd[-1]).write_bytes(b"opus")
        return {"command": cmd, "duration_s": 0.1}

    def fake_build_strict_source_artifacts(**kwargs: object) -> dict[str, object]:
        encode_profile = kwargs["encode_profile"]
        assert isinstance(encode_profile, dict)
        assert encode_profile["source"]["allow_conversion_only_container"] is True
        sidecar = Path(str(kwargs["archive_mkv"]) + ".source-artifacts.tar.zst")
        sidecar.write_bytes(b"source artifacts")
        return {
            "path": str(sidecar),
            "audit": {
                "rebuild_supported": False,
                "rebuild_blockers": ["source_container"],
                "rebuild_scope": (
                    "conversion-only archive; source-container rebuild is not supported"
                ),
            },
        }

    monkeypatch.setattr(server.command_runtime, "run_command", fake_run_command)
    monkeypatch.setattr(
        server.media_service, "build_strict_source_artifacts", fake_build_strict_source_artifacts
    )
    monkeypatch.setattr(
        server.routing_service, "ffprobe_for_routing", lambda _path: {"format": {}, "streams": []}
    )
    monkeypatch.setattr(server.routing_service, "exiftool_for_routing", lambda _path, **_kwargs: {})

    result = server.media_service.run_archive_audio_group(
        input_root=input_root,
        output_root=output_root,
        group_config=group_config,
        provenance_by_rel_path={
            "memo/R-00013_2606222246_REC.MP3": {
                "status": "captured",
                "origin": {"timestamps": {"created": "2026-06-23T05:46:32+00:00"}},
            }
        },
    )
    output = output_root / "memo" / "R-00013_2606222246_REC.opus"
    upload = server.upload_service.save_input_upload_raw(
        {
            "input_upload_id": upload_id,
            "files": [
                {
                    "path": "voice/memo/R-00013_2606222246_REC.MP3",
                    "bytes": 3,
                    "file_upload_id": "file-voice",
                    "input_upload_id": upload_id,
                    "provenance": omitted_test_provenance(),
                    "metadata_projection_metadata": result["items"][0]["container_metadata"],
                    "sha256": "a" * 64,
                }
            ],
        }
    )
    job = {
        "job_id": "job-voice",
        "input_upload_id": upload_id,
        "template_id": "esonic-archive",
    }
    server.state_store.save_job(job)

    updated = server.routing_service.write_metadata_projection_sidecars(
        job,
        upload,
        {"voice": group_config},
        tmp_path / "archive",
    )

    assert result["status"] == "succeeded"
    assert output.read_bytes() == b"opus"
    sidecar = output.with_name("R-00013_2606222246_REC.opus.xmp")
    xmp = sidecar.read_text(encoding="utf-8")
    assert 'exif:DateTimeOriginal="2026-06-23T05:46:32+00:00"' in xmp
    assert 'tiff:Make="eSonic"' in xmp
    assert 'tiff:Model="MEMOQ SR-600"' in xmp
    assert "geo:lat=" not in xmp
    assert "<rdf:li>Example Operator</rdf:li>" in xmp
    assert "<rdf:li>device/esonic-memoq-sr600-example</rdf:li>" in xmp
    assert "<rdf:li>munchy/template/esonic-archive</rdf:li>" in xmp
    assert "<rdf:li>munchy/group/voice</rdf:li>" in xmp
    assert updated["files"][0]["metadata_projection_sidecar"] == (
        "voice/memo/R-00013_2606222246_REC.opus.xmp"
    )


def test_scheduler_reserves_running_job_slots_and_leaves_extra_jobs_queued(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MUNCHY_MAX_RUNNING_JOBS", "1")
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    server.state_store.save_job(
        {
            "job_id": "job-a",
            "state": "queued",
            "phase": "queued",
            "created_at": "2026-01-01T00:00:00Z",
            "handoff": {"destination": "command", "options": {}},
        }
    )
    server.state_store.save_job(
        {
            "job_id": "job-b",
            "state": "queued",
            "phase": "queued",
            "created_at": "2026-01-01T00:00:01Z",
            "handoff": {"destination": "command", "options": {}},
        }
    )
    scheduled_tasks: list[tuple[object, tuple[object, ...]]] = []

    class Tasks:
        def add_task(self, func, *args):  # type: ignore[no-untyped-def]
            scheduled_tasks.append((func, args))

    first = server.job_service.schedule_pending_jobs(Tasks())
    second = server.job_service.schedule_pending_jobs(Tasks())
    job_b = server.processing_service.job_response(server.state_store.load_job("job-b"))

    assert first == ["job-a"]
    assert second == []
    assert scheduled_tasks == [(server.job_service.run_job, ("job-a",))]
    assert server.get_scheduler_status()["scheduled_jobs"] == ["job-a"]
    assert job_b["queue"]["position"] == 2
    assert job_b["queue"]["running_job_limit"] == 1


def test_scheduler_reschedules_a_persisted_running_job_after_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    server.state_store.save_job(
        {
            "job_id": "job-interrupted",
            "state": "running",
            "phase": "handoff",
            "created_at": "2026-01-01T00:00:00Z",
            "started_at": "2026-01-01T00:00:01Z",
            "handoff": {"destination": "command", "options": {}},
        }
    )
    scheduled_tasks: list[tuple[object, tuple[object, ...]]] = []

    class Tasks:
        def add_task(self, func, *args):  # type: ignore[no-untyped-def]
            scheduled_tasks.append((func, args))

    server.execution_runtime.active_jobs.clear()
    server.execution_runtime.scheduled_jobs.clear()

    assert server.job_service.schedule_pending_jobs(Tasks()) == ["job-interrupted"]
    assert scheduled_tasks == [(server.job_service.run_job, ("job-interrupted",))]


def test_submission_accepts_bounded_lifecycle_event_context(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)

    req = server.domain_models.CreateJobRequest(
        handoff={"destination": "riverhog"},
        event_context={"workflow": "desktop-archive", "run": 3},
    )

    assert req.event_context == {"workflow": "desktop-archive", "run": 3}


def test_job_lifecycle_event_is_a_structured_cloudevent(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    job = {
        "job_id": "job-1",
        "template_id": "camera-archive",
        "template_revision": 3,
        "template_digest": "a" * 64,
        "created_at": "2026-07-27T12:34:56.123456Z",
        "initiated_by_app": "desktop-client",
        "initiated_by_key_id": "desktop-key",
        "state": "queued",
        "phase": "queued",
        "workflow_mode": "collection_archive",
        "event_context": {"workflow": "desktop-archive"},
    }
    server.state_store.save_job(job)

    result = server.event_service.emit_job_event(job, "job.received", "received")
    page = server.lifecycle_store.lifecycle_event_log().page(
        after=None,
        limit=100,
        owner="desktop-client",
    )

    assert result["status"] == "emitted"
    assert len(page.events) == 1
    event = page.events[0]
    assert event.specversion == "1.0"
    assert event.source == "urn:munchy"
    assert event.type == "io.riverhog.munchy.job.received"
    assert event.subject == "job-1"
    assert event.data["template_id"] == "camera-archive"
    assert event.data["template_revision"] == 3
    assert event.data["template_digest"] == "a" * 64
    assert event.data["job_created_at"] == "2026-07-27T12:34:56.123456Z"
    assert event.data["context"] == {"workflow": "desktop-archive"}
    assert event.data["initiator"] == {
        "app": "desktop-client",
        "key_id": "desktop-key",
    }


def test_munchy_translates_owned_riverhog_events_idempotently(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    collection_id = 41
    server.state_store.save_job(
        {
            "job_id": "job-1",
            "initiated_by_app": "jeb",
            "initiated_by_key_id": "jeb-key",
            "state": "running",
            "phase": "handoff",
            "workflow_mode": "collection_archive",
            "template_id": "camera-archive",
            "run_id": "20260605T120000Z",
            "handoff": {"destination": "riverhog", "options": {}},
            "handoff_adapter_state": {"collection_id": collection_id},
        }
    )
    upstream = server.cloud_event(
        source="urn:riverhog",
        type="io.riverhog.riverhog.collection.finalized",
        subject=str(collection_id),
        data={
            "collection_id": collection_id,
            "collection_created_at": "2026-06-05T12:00:00.000000Z",
            "collection_tags": ["camera", "family"],
            "archive_store": "b2",
        },
    )

    assert server.riverhog.translate_riverhog_event(upstream)
    assert server.riverhog.translate_riverhog_event(upstream)

    page = server.lifecycle_store.lifecycle_event_log().page(after=None, limit=100, owner="jeb")
    assert len(page.events) == 1
    assert (
        server.lifecycle_store.lifecycle_event_log()
        .page(
            after=None,
            limit=100,
            owner="desktop-client",
        )
        .events
        == []
    )
    event = page.events[0]
    assert event.type == "io.riverhog.munchy.job.archive.finalized"
    assert event.subject == "job-1"
    assert event.data["collection_id"] == collection_id
    assert event.data["collection_created_at"] == "2026-06-05T12:00:00.000000Z"
    assert event.data["collection_tags"] == ["camera", "family"]
    assert event.data["initiator"] == {"app": "jeb", "key_id": "jeb-key"}
    assert event.data["cause"] == {
        "id": upstream.id,
        "source": upstream.source,
        "type": upstream.type,
        "subject": str(collection_id),
    }


def test_munchy_continues_source_history_and_registers_it_before_the_output(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    monkeypatch.setattr(server.riverhog, "RIVERHOG_HANDOFF_ENABLED", True)
    upload_id = "captured-input"
    source = tmp_path / "source.mov"
    source.write_bytes(b"original container")
    source_journal = create_observation_journal(
        source,
        relative_path="video/source.mov",
        host_id="urn:uuid:00000000-0000-4000-8000-000000000001",
        agent_name="munchy-client",
        agent_version="1.0.0",
    )
    source_summary = validate_journal(source_journal)
    server.upload_service.save_input_upload_raw(
        {
            "input_upload_id": upload_id,
            "files": [
                {
                    "input_upload_id": upload_id,
                    "file_upload_id": "source-file",
                    "path": "video/source.mov",
                    "bytes": source.stat().st_size,
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "resolved_group": "video",
                    "resolved_group_rel": "source.mov",
                    "route_action": "upload",
                    "provenance": {
                        "status": "captured",
                        "journal_id": source_summary.journal_id,
                        "current_state_id": source_summary.current_state_id,
                    },
                }
            ],
        }
    )
    server.upload_service.put_input_provenance_journal(
        upload_id,
        source_summary.journal_id,
        content=source_journal,
        sha256=source_summary.journal_sha256,
    )
    archive_dir = tmp_path / "archive"
    output = archive_dir / "video" / "source.mkv"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"canonical transformed container")
    job = {
        "job_id": "captured-job",
        "input_upload_id": upload_id,
        "started_at": "2026-08-10T01:00:00.000000Z",
        "groups": {
            "video": {
                "output_mode": "video",
                "tasks": ["archive_video"],
            }
        },
        "handoff": {"destination": "riverhog", "options": {}},
        "handoff_adapter_state": {"collection_id": 41, "files": {}},
    }
    server.state_store.save_job(job)

    provenance, bindings, journals = server.riverhog.munchy_output_provenance(job, archive_dir)
    binding = bindings[0]
    continued = journals[str(binding.journal_id)]
    current = validate_journal(continued)

    assert provenance is not None
    assert binding.status == "captured"
    assert binding.path == "video/source.mkv"
    assert continued.startswith(source_journal)
    assert current.journal_id == source_summary.journal_id
    assert current.primary_lineage_id == source_summary.primary_lineage_id
    assert current.current_path == "video/source.mkv"
    assert current.current_sha256 == hashlib.sha256(output.read_bytes()).hexdigest()

    calls: list[str] = []

    class FakeApi:
        def put_collection_upload_session_provenance_journal(
            self,
            collection_id: int,
            journal_id: str,
            *,
            content: bytes,
            sha256: str,
        ) -> None:
            assert collection_id == 41
            assert journal_id == current.journal_id
            assert content == continued
            assert sha256 == current.journal_sha256
            calls.append("provenance")

        def register_collection_upload_session_files(
            self,
            collection_id: int,
            files: tuple[dict[str, object], ...],
        ) -> dict[str, object]:
            assert collection_id == 41
            assert files[0]["path"] == "video/source.mkv"
            assert files[0]["provenance"] == {
                "status": "captured",
                "journal_id": current.journal_id,
                "current_state_id": current.current_state_id,
            }
            calls.append("binding")
            return {"collection_id": 41, "state": "open"}

    registered = server.riverhog.register_riverhog_artifacts(job, FakeApi(), archive_dir)

    assert calls == ["provenance", "binding"]
    assert registered["registered_files"] == 1


def test_riverhog_event_maps_to_the_latest_job_started_before_the_event(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    collection_id = 41
    for job_id, created_at in (
        ("job-1", "2026-06-05T12:00:00.000000Z"),
        ("job-2", "2026-06-05T13:00:00.000000Z"),
    ):
        server.state_store.save_job(
            {
                "job_id": job_id,
                "created_at": created_at,
                "template_id": "camera-archive",
                "run_id": "20260605T120000Z",
                "handoff_adapter_state": {"collection_id": collection_id},
            }
        )

    earlier = server.riverhog.job_for_riverhog_collection(
        collection_id,
        event_time="2026-06-05T12:30:00.000000Z",
    )
    later = server.riverhog.job_for_riverhog_collection(
        collection_id,
        event_time="2026-06-05T13:30:00.000000Z",
    )

    assert earlier is not None and earlier["job_id"] == "job-1"
    assert later is not None and later["job_id"] == "job-2"
    with sqlite3.connect(server.runtime_config.STATE_DB_PATH) as conn:
        indexes = {str(row[1]) for row in conn.execute("PRAGMA index_list(states)")}
    assert {
        "riverhog_jobs_adapter_collection",
        "riverhog_jobs_receipt_collection",
        "riverhog_jobs_progress_collection",
    } <= indexes


def test_missing_remote_handoff_artifact_fails_without_retrying_forever(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    monkeypatch.setattr(server.riverhog, "RIVERHOG_HANDOFF_ENABLED", True)
    monkeypatch.setattr(
        server.riverhog,
        "munchy_output_provenance",
        lambda *_args: (None, (), {}),
    )
    collection_id = 41
    job = {
        "job_id": "job-1",
        "state": "running",
        "phase": "handoff",
        "template_id": "camera-archive",
        "run_id": "20260605T120000Z",
        "handoff": {"destination": "riverhog", "options": {}},
        "handoff_adapter_state": {
            "collection_id": collection_id,
            "files": {
                "camera/a.webm": {
                    "path": "camera/a.webm",
                    "bytes": 10,
                    "sha256": "a" * 64,
                    "uploaded_bytes": 10,
                    "state": "deleted",
                }
            },
        },
    }
    server.state_store.save_job(job)

    class MissingApi:
        def complete_collection_upload_session(self, requested: str, **_kwargs: object):
            assert requested == collection_id
            raise Conflict("collection upload session still has missing file bytes")

        def get_collection_upload_session(self, requested: str):
            assert requested == collection_id
            return {
                "collection_id": collection_id,
                "state": "open",
            }

        def list_collection_upload_session_files(self, requested: str, **_kwargs: object):
            assert requested == collection_id
            return {
                "page": 1,
                "pages": 1,
                "files": [
                    {
                        "path": "camera/a.webm",
                        "bytes": 10,
                        "uploaded_bytes": 0,
                        "upload_state": "pending",
                    }
                ],
            }

    with pytest.raises(
        server.domain_errors.HandoffFailed,
        match=r"missing registered files \(1\); local handoff artifacts unavailable \(1\)",
    ):
        server.riverhog.complete_riverhog_session(job, MissingApi(), tmp_path / "archive")

    stored = server.state_store.load_job("job-1")
    record = stored["handoff_adapter_state"]["files"]["camera/a.webm"]
    assert record["uploaded_bytes"] == 0
    assert record["state"] == "missing"


def test_job_issue_event_carries_operational_facts_without_presentation(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    job = {
        "job_id": "job-1",
        "template_id": "camera-archive",
        "run_id": "20260605T120000Z",
        "phase": "queued",
        "state": "queued",
    }
    server.state_store.save_job(job)

    result = server.event_service.emit_job_issue(
        job,
        component="routing",
        error="route could not be resolved",
        severity="critical",
    )
    event = server.lifecycle_store.lifecycle_event_log().page(after=None, limit=100).events[0]

    assert result["status"] == "emitted"
    assert event.type == "io.riverhog.munchy.job.issue"
    assert event.data["component"] == "routing"
    assert event.data["error"] == "route could not be resolved"
    assert event.data["severity"] == "critical"


def test_submission_preflight_failure_is_recorded_as_a_lifecycle_event(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()

    result = server.record_submission_preflight_failure(
        server.domain_models.SubmissionPreflightFailureCreate(
            submission_id="submission-1",
            message="Local media preflight failed for camera.",
            template_id="camera-archive",
            workflow_mode="collection_archive",
            group="camera-video",
            run_id="20260606T120000Z",
            files_total=2,
            failed_files_total=1,
            failed_files=[
                {
                    "path": "camera-video/bad.mp4",
                    "source": "/source/bad.mp4",
                    "issues": [{"code": "mp4_atom_extends_past_eof", "message": "bad atom"}],
                }
            ],
            event_context={"workflow": "desktop-archive"},
        ),
        _request_with_principal(server, app="desktop-client", key_id="key-1"),
    )
    event = server.lifecycle_store.lifecycle_event_log().page(after=None, limit=100).events[0]

    assert result["status"] == "recorded"
    assert event.type == "io.riverhog.munchy.submission.preflight_failed"
    assert event.subject == "submission-1"
    assert event.data["component"] == "preflight"
    assert event.data["template_id"] == "camera-archive"
    assert event.data["error"] == "bad atom (bad.mp4)"
    assert event.data["failed_files_total"] == 1
    assert event.data["context"] == {"workflow": "desktop-archive"}


def test_upload_stalled_event_is_time_sensitive_and_paced(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MUNCHY_EVENT_REPEAT_INTERVAL", "1")
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    job = {
        "job_id": "job-1",
        "template_id": "camera-archive",
        "run_id": "20260606T120000Z",
        "state": "running",
        "phase": "waiting_for_eager_files:1/2",
    }
    server.state_store.save_job(job)
    upload = {
        "input_upload_id": "upload-1",
        "created_at": "2026-01-01T00:00:00Z",
        "files": [
            {
                "path": "camera/a.mp4",
                "bytes": 1,
                "file_upload_id": "upload-a",
                "provenance": omitted_test_provenance(),
                "sha256": "a" * 64,
            },
            {
                "path": "camera/b.mp4",
                "bytes": 1,
                "file_upload_id": "upload-b",
                "provenance": omitted_test_provenance(),
                "sha256": "a" * 64,
            },
        ],
    }
    progress = {
        "files_total": 2,
        "files_uploaded": 1,
        "bytes_total": 2,
        "uploaded_bytes": 1,
    }

    first = server.processing_service.emit_upload_stalled(job, upload, progress)
    second = server.processing_service.emit_upload_stalled(job, upload, progress)
    event = server.lifecycle_store.lifecycle_event_log().page(after=None, limit=100).events[0]

    assert first["status"] == "emitted"
    assert second["status"] == "suppressed"
    assert second["reason"] == "reminder_repeat_limit"
    assert event.type == "io.riverhog.munchy.job.upload_stalled"
    assert event.data["severity"] == "warning"
    assert event.data["repeat_count"] == 1
    assert event.data["repeat_interval_seconds"] == 1
    assert event.data["upload_progress"]["files_uploaded"] == 1


def test_retry_handoff_transient_failure_does_not_emit_event(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    monkeypatch.setattr(server.handoff_service, "retry_sleep", lambda *args, **kwargs: None)
    event_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        server.event_service,
        "emit_job_issue",
        lambda *args, **kwargs: event_calls.append(dict(kwargs)),
    )
    job = {
        "job_id": "job-1",
        "state": "running",
        "phase": "handoff",
        "handoff": {"destination": "riverhog", "options": {}},
    }
    server.state_store.save_job(job)
    attempts = 0

    def operation() -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("Connection reset by peer")
        return {"ok": True}

    result = server.handoff_service.retry_handoff_until_success(
        job,
        result_key="handoff_receipt",
        phase="handoff",
        action="Riverhog handoff",
        component="riverhog_handoff",
        operation=operation,
    )

    assert result["ok"] is True
    assert result["attempt"] == 2
    assert isinstance(result["succeeded_at"], str)
    assert event_calls == []


def test_eager_gpu_transient_failure_does_not_emit_event(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    event_calls: list[dict[str, object]] = []
    submit_calls: list[str] = []
    monkeypatch.setattr(
        server.event_service,
        "emit_job_issue",
        lambda *args, **kwargs: event_calls.append(dict(kwargs)),
    )

    def fail_gpu_request(method: str, path: str, payload=None):  # type: ignore[no-untyped-def]
        raise RuntimeError("Server disconnected without sending a response.")

    monkeypatch.setattr(server.media_service, "gpu_target_request", fail_gpu_request)
    monkeypatch.setattr(
        server.processing_service,
        "submit_eager_gpu_job",
        lambda job, batch, force=False: submit_calls.append(str(batch["gpu_job_id"])),
    )
    job = {"job_id": "job-1"}
    upload = {"input_upload_id": "upload-1"}
    batch = {
        "batch_id": "batch-1",
        "gpu_job_id": "gpu-1",
        "payload": {"job_id": "gpu-1"},
    }

    result = server.processing_service.poll_eager_gpu_batch(
        job, upload, batch, {}, tmp_path / "archive"
    )

    assert result is upload
    assert submit_calls == ["gpu-1"]
    assert event_calls == []


def test_terminal_encoding_failure_with_retained_input_reports_error(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    job = {"job_id": "job-1", "state": "running", "phase": "encoding"}
    server.state_store.save_job(job)
    events: list[dict[str, object]] = []
    monkeypatch.setattr(
        server.media_service,
        "gpu_target_request",
        lambda *args, **kwargs: {"state": "failed", "error": "encoder refused"},
    )
    monkeypatch.setattr(
        server.event_service,
        "emit_job_issue",
        lambda *args, **kwargs: events.append(dict(kwargs)),
    )

    with pytest.raises(server.domain_errors.EncodingFailed, match="encoder refused"):
        server.media_service.wait_gpu_job("gpu-1", gpu_payload={}, job=job)

    assert events == [
        {
            "component": "encoding",
            "error": "gpu job failed: encoder refused",
            "severity": "error",
        }
    ]


def test_load_input_upload_does_not_refresh_state_timestamp(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    server.upload_service.save_input_upload(
        {
            "input_upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "files": [
                {
                    "path": "camera/a.mp4",
                    "bytes": 1,
                    "file_upload_id": "upload-a",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                }
            ],
        }
    )
    before = server.state_store.read_state("input-upload", "upload-1")["updated_at"]

    loaded = server.upload_service.load_input_upload("upload-1")

    after = server.state_store.read_state("input-upload", "upload-1")["updated_at"]
    assert loaded["files_total"] == 1
    assert after == before


def test_resume_job_clears_failed_runtime_state(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    canceled: list[str] = []
    monkeypatch.setattr(server.job_service, "schedule_pending_jobs", lambda _background_tasks: None)
    monkeypatch.setattr(
        server.riverhog,
        "cancel_riverhog_upload_session",
        lambda job, reason: canceled.append(f"{job['job_id']}:{reason}"),
    )
    server.state_store.save_job(
        {
            "job_id": "job-1",
            "state": "failed",
            "phase": "eager_archive:pipeline=1/3",
            "input_upload_id": "upload-1",
            "error": "old error",
            "finished_at": "2026-01-01T00:00:00Z",
            "handoff": {"destination": "riverhog", "options": {}},
            "groups": {"camera": {"output_mode": "video", "tasks": ["archive_video"]}},
            "routing_result": {"files": 1},
            "eager_archive": {"files": {"camera/a.mp4": {"state": "failed"}}},
            "handoff_adapter_state": {"collection_id": "collection-1", "files": {}},
        }
    )
    diagnostic = server.diagnostic_service.create_job_diagnostic(
        server.state_store.load_job("job-1"),
        reason="job_failed",
    )
    assert diagnostic is not None
    diagnostic_path = Path(server.state_store.read_job_diagnostic("job-1")["path"])

    resumed = server.resume_job("job-1", server.BackgroundTasks())
    stored = server.state_store.load_job("job-1")

    assert resumed["state"] == "queued"
    assert stored["phase"] == "queued"
    assert canceled == ["job-1:job_resume_reset"]
    assert "error" not in stored
    assert "finished_at" not in stored
    assert "eager_archive" not in stored
    assert "handoff_adapter_state" not in stored
    assert "routing_result" not in stored
    assert server.state_store.read_job_diagnostic("job-1") is None
    assert not diagnostic_path.exists()
    assert stored["groups"] == {"camera": {"output_mode": "video", "tasks": ["archive_video"]}}


def test_resume_job_preserves_fully_uploaded_riverhog_session(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    monkeypatch.setattr(server.job_service, "schedule_pending_jobs", lambda _background_tasks: None)
    monkeypatch.setattr(server.riverhog, "riverhog_session_visible_for_resume", lambda _job: True)
    monkeypatch.setattr(
        server.riverhog,
        "cancel_riverhog_upload_session",
        lambda _job, _reason: (_ for _ in ()).throw(
            AssertionError("resume should preserve a fully uploaded Riverhog session")
        ),
    )
    riverhog_session = {
        "collection_id": 43,
        "state": "open",
        "files": {
            "camera/a.webm": {
                "path": "camera/a.webm",
                "bytes": 7,
                "uploaded_bytes": 7,
                "state": "uploaded",
            },
            "camera/a.source-artifacts.tar.zst": {
                "path": "camera/a.source-artifacts.tar.zst",
                "bytes": 3,
                "uploaded_bytes": 3,
                "state": "uploaded",
            },
        },
    }
    server.state_store.save_job(
        {
            "job_id": "job-1",
            "state": "failed",
            "phase": "copying_preserve:preserve",
            "input_upload_id": "upload-1",
            "template_id": "weekly-device-artifacts-archive",
            "run_id": "20260615T030000Z",
            "workflow_mode": "collection_archive",
            "handoff": {"destination": "riverhog", "options": {}},
            "error": "old error",
            "finished_at": "2026-01-01T00:00:00Z",
            "eager_archive": {"files": {"camera/a.mp4": {"state": "encoded"}}},
            "handoff_adapter_state": riverhog_session,
        }
    )
    diagnostic = server.diagnostic_service.create_job_diagnostic(
        server.state_store.load_job("job-1"),
        reason="job_failed",
    )
    assert diagnostic is not None
    diagnostic_path = Path(server.state_store.read_job_diagnostic("job-1")["path"])

    resumed = server.resume_job("job-1", server.BackgroundTasks())
    stored = server.state_store.load_job("job-1")

    assert resumed["state"] == "queued"
    assert stored["phase"] == "queued"
    assert "error" not in stored
    assert "finished_at" not in stored
    assert server.state_store.read_job_diagnostic("job-1") is None
    assert not diagnostic_path.exists()
    assert stored["eager_archive"]["files"] == {"camera/a.mp4": {"state": "encoded"}}
    assert stored["handoff_adapter_state"]["files"] == riverhog_session["files"]
    assert stored["handoff_resume_preserved_at"]


def test_review_rclone_upload_excludes_platform_cruft(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MUNCHY_EXTERNAL_HANDOFF_ENABLED", "1")
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    monkeypatch.setattr(
        server.handoff_service,
        "retry_handoff_until_success",
        lambda _job, **kwargs: kwargs["operation"](),
    )
    source_dir = tmp_path / "review"
    source_dir.mkdir()
    (source_dir / ".DS_Store").write_bytes(b"finder")
    (source_dir / "clip.webm").write_bytes(b"video")
    nested = source_dir / "nested"
    nested.mkdir()
    (nested / "._clip.webm").write_bytes(b"appledouble")
    commands: list[list[str]] = []

    def fake_run_handoff_command(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        commands.append(list(cmd))
        return {"returncode": 0}

    monkeypatch.setattr(server.command_runtime, "run_command", fake_run_handoff_command)

    result = server.external.run_external_handoff(
        {
            "job_id": "job-1",
            "template_id": "collection-archive",
            "run_id": "20260629T000000Z",
        },
        source_dir,
        config={
            "enabled": True,
            "method": "rclone",
            "mode": "copy",
            "destination": "remote:reviews/{run_id}",
        },
        source_label="collection archive",
        result_key="handoff_receipt",
        phase="handoff",
        component="handoff",
        event="collection_archive.handoff",
    )

    assert result["artifact_count"] == 1
    assert result["destination"] == "rclone"
    assert result["location"] == "remote:reviews/20260629T000000Z"
    command = commands[0]
    assert command[:2] == ["rclone", "copy"]
    assert str(source_dir) in command
    assert "remote:reviews/20260629T000000Z" in command
    assert command.index(str(source_dir)) < command.index("remote:reviews/20260629T000000Z")
    assert command.count("--exclude") >= 4
    assert ".DS_Store" in command
    assert "**/._*" in command


def test_cancel_job_with_cleanup_removes_local_work_and_input_upload(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    data_path = server.upload_service.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"a")
    shared_root = server.upload_service.shared_input_upload_root("upload-1")
    shared_root.mkdir(parents=True)
    (shared_root / "camera").mkdir()
    (shared_root / "camera" / "a.mp4").write_bytes(b"a")
    work_dir = server.runtime_config.GPU_RUNTIME_DIR / "jobs" / "job-1"
    work_dir.mkdir(parents=True)
    (work_dir / "scratch.txt").write_text("scratch", encoding="utf-8")
    server.upload_service.save_input_upload(
        {
            "input_upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "files": [
                {
                    "path": "camera/a.mp4",
                    "bytes": 1,
                    "file_upload_id": "upload-a",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                }
            ],
        }
    )
    server.state_store.save_job(
        {
            "job_id": "job-1",
            "state": "queued",
            "phase": "queued",
            "input_upload_id": "upload-1",
            "groups": {"camera": {}},
            "handoff": {"destination": "command", "options": {}},
        }
    )

    job = server.cancel_job("job-1", cleanup=True)

    assert job["state"] == "canceled"
    assert job["cleanup_requested"] is True
    assert server.state_store.read_state("input-upload", "upload-1") is None
    assert not data_path.exists()
    assert not shared_root.exists()
    assert not work_dir.exists()
    assert "input-upload:upload-1" in job["cleanup_removed"]


def test_cancel_job_with_cleanup_preserves_input_upload_referenced_by_sibling(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    data_path = server.upload_service.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"a")
    shared_root = server.upload_service.shared_input_upload_root("upload-1")
    shared_root.mkdir(parents=True)
    server.upload_service.save_input_upload(
        {
            "input_upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "files": [
                {
                    "path": "camera/a.mp4",
                    "bytes": 1,
                    "file_upload_id": "upload-a",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                }
            ],
        }
    )
    server.state_store.save_job(
        {
            "job_id": "job-1",
            "state": "queued",
            "phase": "queued",
            "input_upload_id": "upload-1",
            "groups": {"camera": {}},
            "handoff": {"destination": "command", "options": {}},
        }
    )
    server.state_store.save_job(
        {
            "job_id": "job-2",
            "state": "queued",
            "phase": "queued",
            "input_upload_id": "upload-1",
            "groups": {"camera": {}},
            "handoff": {"destination": "command", "options": {}},
        }
    )

    job = server.cancel_job("job-1", cleanup=True)

    assert job["state"] == "canceled"
    assert server.state_store.read_state("input-upload", "upload-1") is not None
    assert data_path.exists()
    assert shared_root.exists()
    assert "input-upload:upload-1" not in job.get("cleanup_removed", [])


def test_finalize_canceled_job_persists_terminal_state_before_cleanup_failures(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    data_path = server.upload_service.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"a")
    shared_root = server.upload_service.shared_input_upload_root("upload-1")
    shared_root.mkdir(parents=True)
    (shared_root / "camera").mkdir()
    (shared_root / "camera" / "a.mp4").write_bytes(b"a")
    work_dir = server.runtime_config.GPU_RUNTIME_DIR / "jobs" / "job-1"
    work_dir.mkdir(parents=True)
    (work_dir / "scratch.txt").write_text("scratch", encoding="utf-8")
    server.upload_service.save_input_upload(
        {
            "input_upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "files": [
                {
                    "path": "camera/a.mp4",
                    "bytes": 1,
                    "file_upload_id": "upload-a",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                }
            ],
        }
    )
    job = server.state_store.save_job(
        {
            "job_id": "job-1",
            "state": "running",
            "phase": "cancel_requested",
            "cancel_requested": True,
            "input_upload_id": "upload-1",
            "groups": {"camera": {}},
            "handoff": {"destination": "riverhog", "options": {}},
            "handoff_adapter_state": {
                "state": "open",
                "collection_id": 42,
                "files": {},
            },
        }
    )

    def fail_cancel(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("riverhog cancel unavailable")

    monkeypatch.setattr(server.riverhog, "cancel_riverhog_upload_session", fail_cancel)

    finalized = server.cleanup_service.finalize_canceled_job(job, reason="test_cancel")
    stored = server.state_store.load_job("job-1")

    assert finalized["state"] == "canceled"
    assert stored["state"] == "canceled"
    assert stored["phase"] == "canceled"
    assert "cancel_requested" not in stored
    assert stored["handoff_cancel_failed_at"]
    assert stored["handoff_cancel_error"] == "handoff cancellation failed"
    assert server.state_store.read_state("input-upload", "upload-1") is None
    assert not data_path.exists()
    assert not shared_root.exists()
    assert not work_dir.exists()


def test_failed_job_with_cleanup_removes_local_work_and_input_upload(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    data_path = server.upload_service.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"a")
    shared_root = server.upload_service.shared_input_upload_root("upload-1")
    shared_root.mkdir(parents=True)
    (shared_root / "camera").mkdir()
    (shared_root / "camera" / "a.mp4").write_bytes(b"a")
    work_dir = server.runtime_config.GPU_RUNTIME_DIR / "jobs" / "job-1"
    work_dir.mkdir(parents=True)
    (work_dir / "scratch.txt").write_text("scratch", encoding="utf-8")
    server.upload_service.save_input_upload(
        {
            "input_upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "files": [
                {
                    "path": "camera/a.mp4",
                    "bytes": 1,
                    "file_upload_id": "upload-a",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                }
            ],
        }
    )
    server.state_store.save_job(
        {
            "job_id": "job-1",
            "state": "failed",
            "phase": "gpu",
            "finished_at": "2026-01-01T00:00:00Z",
            "input_upload_id": "upload-1",
            "upload_progress": {"files_total": 1, "files_uploaded": 1},
            "groups": {"camera": {}},
            "handoff": {"destination": "command", "options": {}},
        }
    )

    job = server.cancel_job("job-1", cleanup=True)

    assert job["state"] == "failed"
    assert server.state_store.read_state("input-upload", "upload-1") is None
    assert not data_path.exists()
    assert not shared_root.exists()
    assert not work_dir.exists()
    assert "input-upload:upload-1" in job["cleanup_removed"]
    assert job["cleanup_completed_at"]
    assert job["input_upload_deleted_at"]
    assert job["upload_progress"] == {"files_total": 1, "files_uploaded": 1}


def test_terminal_cleanup_removes_eager_batch_gpu_work_roots(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    computed_id = server.routing_service.gpu_eager_batch_job_id("job-1", "batch-2")
    roots = [
        server.runtime_config.GPU_RUNTIME_DIR / "jobs" / "job-1",
        server.runtime_config.GPU_RUNTIME_DIR / "jobs" / "explicit-eager-gpu-job",
        server.runtime_config.GPU_RUNTIME_DIR / "jobs" / "payload-eager-gpu-job",
        server.runtime_config.GPU_RUNTIME_DIR / "jobs" / computed_id,
        server.runtime_config.GPU_RUNTIME_DIR / "jobs" / "job-1__eager__orphaned-batch__abcdef0123",
    ]
    for root in roots:
        root.mkdir(parents=True)
        (root / "scratch.txt").write_text("scratch", encoding="utf-8")
    job = {
        "job_id": "job-1",
        "state": "failed",
        "phase": "gpu",
        "handoff": {"destination": "command", "options": {}},
        "eager_archive": {
            "batches": {
                "batch-1": {
                    "batch_id": "batch-1",
                    "gpu_job_id": "explicit-eager-gpu-job",
                    "payload": {"job_id": "payload-eager-gpu-job"},
                },
                "batch-2": {"batch_id": "batch-2"},
            }
        },
    }

    removed = server.cleanup_service.cleanup_terminal_job(job)

    assert len(removed) == len(roots)
    for root in roots:
        assert not root.exists()
        assert str(root) in removed


def test_cleanup_once_removes_old_failed_job_work_and_input_upload(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    data_path = server.upload_service.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"a")
    shared_root = server.upload_service.shared_input_upload_root("upload-1")
    shared_root.mkdir(parents=True)
    (shared_root / "camera").mkdir()
    (shared_root / "camera" / "a.mp4").write_bytes(b"a")
    work_dir = server.runtime_config.GPU_RUNTIME_DIR / "jobs" / "job-1"
    work_dir.mkdir(parents=True)
    (work_dir / "scratch.txt").write_text("scratch", encoding="utf-8")
    server.upload_service.save_input_upload(
        {
            "input_upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "files": [
                {
                    "path": "camera/a.mp4",
                    "bytes": 1,
                    "file_upload_id": "upload-a",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                }
            ],
        }
    )
    server.state_store.save_job(
        {
            "job_id": "job-1",
            "state": "failed",
            "phase": "gpu",
            "finished_at": "2026-01-01T00:00:00Z",
            "input_upload_id": "upload-1",
            "groups": {"camera": {}},
            "handoff": {"destination": "command", "options": {}},
        }
    )
    canceled: list[str] = []
    monkeypatch.setattr(
        server.handoff_service,
        "cancel_handoff",
        lambda _job, *, reason: canceled.append(reason),
    )

    result = server.job_service.cleanup_once()

    assert "job-cleanup:job-1" in result["removed"]
    assert canceled == ["terminal_cleanup"]
    assert server.state_store.read_state("input-upload", "upload-1") is None
    assert not data_path.exists()
    assert not shared_root.exists()
    assert not work_dir.exists()


def test_input_upload_cleanup_candidates_are_bounded_and_exclude_active_job_inputs(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.persistence.initialize_persistence()
    for upload_id in ("active", "orphan-a", "orphan-b"):
        server.state_store.write_state(
            "input-upload",
            upload_id,
            {
                "input_upload_id": upload_id,
                "created_at": "2026-01-01T00:00:00Z",
                "files": [],
            },
        )
    server.state_store.save_job(
        {
            "job_id": "active-job",
            "state": "queued",
            "input_upload_id": "active",
        }
    )

    candidates = server.state_store.unreferenced_input_upload_states(limit=2)

    assert [candidate["input_upload_id"] for candidate in candidates] == [
        "orphan-a",
        "orphan-b",
    ]


def test_cleanup_once_repairs_stale_cancel_requested_job(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    data_path = server.upload_service.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"a")
    shared_root = server.upload_service.shared_input_upload_root("upload-1")
    shared_root.mkdir(parents=True)
    (shared_root / "camera").mkdir()
    (shared_root / "camera" / "a.mp4").write_bytes(b"a")
    work_dir = server.runtime_config.GPU_RUNTIME_DIR / "jobs" / "job-1"
    work_dir.mkdir(parents=True)
    (work_dir / "scratch.txt").write_text("scratch", encoding="utf-8")
    server.upload_service.save_input_upload(
        {
            "input_upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "files": [
                {
                    "path": "camera/a.mp4",
                    "bytes": 1,
                    "file_upload_id": "upload-a",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                }
            ],
        }
    )
    server.state_store.save_job(
        {
            "job_id": "job-1",
            "state": "running",
            "phase": "cancel_requested",
            "cancel_requested": True,
            "input_upload_id": "upload-1",
            "groups": {"camera": {}},
            "handoff": {"destination": "command", "options": {}},
        }
    )

    result = server.job_service.cleanup_once()
    job = server.state_store.load_job("job-1")

    assert result["repaired_canceled"] == ["job-1"]
    assert job["state"] == "canceled"
    assert job["phase"] == "canceled"
    assert "cancel_requested" not in job
    assert job["cancel_reason"] == "stale_cancel_requested"
    assert server.state_store.read_state("input-upload", "upload-1") is None
    assert not data_path.exists()
    assert not shared_root.exists()
    assert not work_dir.exists()


def test_cancel_cleanup_snapshots_partial_encode_totals_before_input_deletion(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    for upload_id, _path in (("upload-a", "camera/a.mp4"), ("upload-b", "camera/b.mp4")):
        data_path = server.upload_service.tusd_data_path(upload_id)
        data_path.parent.mkdir(parents=True, exist_ok=True)
        data_path.write_bytes(b"a")
    shared_root = server.upload_service.shared_input_upload_root("upload-1")
    (shared_root / "camera").mkdir(parents=True)
    (shared_root / "camera" / "a.mp4").write_bytes(b"a")
    (shared_root / "camera" / "b.mp4").write_bytes(b"a")
    output = (
        server.runtime_config.GPU_RUNTIME_DIR / "jobs" / "job-1" / "archive" / "camera" / "a.webm"
    )
    output.parent.mkdir(parents=True)
    output.write_bytes(b"encoded")
    server.upload_service.save_input_upload(
        {
            "input_upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "files": [
                {
                    "path": "camera/a.mp4",
                    "bytes": 1,
                    "file_upload_id": "upload-a",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                },
                {
                    "path": "camera/b.mp4",
                    "bytes": 1,
                    "file_upload_id": "upload-b",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                },
            ],
        }
    )
    job = server.state_store.save_job(
        {
            "job_id": "job-1",
            "state": "running",
            "phase": "cancel_requested",
            "cancel_requested": True,
            "input_upload_id": "upload-1",
            "handoff": {"destination": "command", "options": {}},
            "groups": {
                "camera": {
                    "output_mode": "video",
                    "tasks": ["archive_video"],
                }
            },
            "eager_archive": {
                "files": {
                    "camera/a.mp4": {
                        "state": "encoded",
                        "group": "camera",
                        "input_bytes": 1,
                        "output": str(output),
                        "output_bytes": 7,
                    }
                },
                "batches": {},
                "next_batch_number": 1,
            },
        }
    )

    server.cleanup_service.finalize_canceled_job(job, reason="test_cancel")
    stored = server.state_store.load_job("job-1")

    assert stored["state"] == "canceled"
    assert stored["encode_progress"]["files_total"] == 2
    assert stored["encode_progress"]["files_encoded"] == 1
    assert stored["upload_progress"]["files_total"] == 2
    assert server.state_store.read_state("input-upload", "upload-1") is None


def test_compact_job_response_includes_cleanup_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    job = {
        "job_id": "job-1",
        "state": "failed",
        "phase": "failed",
        "handoff": {"destination": "command", "options": {}},
        "cleanup_removed": ["input-upload:upload-1"],
        "cleanup_completed_at": "2026-01-01T00:00:00Z",
        "input_upload_deleted_at": "2026-01-01T00:00:00Z",
        "local_work_cleaned_at": "2026-01-01T00:00:00Z",
        "local_work_removed": ["/gpu/jobs/job-1"],
        "eager_archive": {"large": ["payload"]},
    }

    compact = server.processing_service.compact_job_response(job)

    assert compact["cleanup_removed"] == ["input-upload:upload-1"]
    assert compact["cleanup_completed_at"] == "2026-01-01T00:00:00Z"
    assert compact["input_upload_deleted_at"] == "2026-01-01T00:00:00Z"
    assert compact["local_work_removed"] == ["/gpu/jobs/job-1"]
    assert "eager_archive" not in compact


def test_save_job_preserves_terminal_cleanup_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    server.state_store.save_job(
        {
            "job_id": "job-1",
            "state": "canceled",
            "phase": "canceled",
            "handoff": {"destination": "command", "options": {}},
            "cleanup_completed_at": "2026-01-01T00:00:00Z",
            "cleanup_removed_count": 25,
            "cleanup_removed_sample": ["job-work:job-1"],
            "input_upload_deleted_at": "2026-01-01T00:00:00Z",
        }
    )

    server.state_store.save_job(
        {
            "job_id": "job-1",
            "state": "canceled",
            "phase": "canceled",
            "finished_at": "2026-01-01T00:00:01Z",
        }
    )

    stored = server.state_store.load_job("job-1")
    assert stored["cleanup_completed_at"] == "2026-01-01T00:00:00Z"
    assert stored["cleanup_removed_count"] == 25
    assert stored["cleanup_removed_sample"] == ["job-work:job-1"]
    assert stored["input_upload_deleted_at"] == "2026-01-01T00:00:00Z"


def test_cleanup_terminal_job_records_empty_cleanup_completion(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    job = {
        "job_id": "job-1",
        "state": "canceled",
        "phase": "canceled",
        "handoff": {"destination": "command", "options": {}},
    }

    removed = server.cleanup_service.cleanup_terminal_job(job)
    server.cleanup_service.compact_terminal_job_state(job)

    assert removed == []
    assert job["cleanup_completed_at"]
    assert job["cleanup_removed_count"] == 0


def test_cleanup_terminal_job_preserves_repeat_cleanup_summary(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    job = {
        "job_id": "job-1",
        "state": "canceled",
        "phase": "canceled",
        "handoff": {"destination": "command", "options": {}},
        "cleanup_removed": [f"/gpu/jobs/job-1-{index}" for index in range(12)],
        "cleanup_completed_at": "2026-01-01T00:00:00Z",
    }
    server.cleanup_service.compact_terminal_job_state(job)

    removed = server.cleanup_service.cleanup_terminal_job(job)
    server.cleanup_service.compact_terminal_job_state(job)

    assert removed == []
    assert job["cleanup_completed_at"] != "2026-01-01T00:00:00Z"
    assert job["cleanup_removed_count"] == 12
    assert len(job["cleanup_removed_sample"]) == 8
    assert "cleanup_removed" not in job


def test_compact_terminal_job_state_keeps_summaries_and_drops_heavy_payloads(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    job = {
        "job_id": "job-1",
        "state": "succeeded",
        "phase": "done",
        "workflow_mode": "collection_archive",
        "input_upload_id": "upload-1",
        "handoff": {"destination": "rclone", "options": {"location": "remote:path"}},
        "groups": {
            "camera": {
                "output_mode": "video",
                "tasks": ["archive_video"],
            }
        },
        "eager_archive": {
            "files": {
                "camera/a.mp4": {
                    "state": "encoded",
                    "group": "camera",
                    "input_bytes": 100,
                    "output_bytes": 10,
                    "started_at": "2026-01-01T00:00:00Z",
                    "encoded_at": "2026-01-01T00:00:10Z",
                }
            },
            "batches": {
                "batch-1": {
                    "state": "succeeded",
                    "started_at": "2026-01-01T00:00:00Z",
                    "finished_at": "2026-01-01T00:00:10Z",
                    "payload": {"large": "payload"},
                    "gpu_result": {"large": "result"},
                }
            },
            "gpu_results": {"batch-1": {"large": "result"}},
        },
        "gpu_payloads": {"camera": {"large": "payload"}},
        "gpu_result": {"large": "result"},
        "gpu_results": {"camera": {"large": "result"}},
        "gpu_statuses": {"gpu-job": {"state": "succeeded"}},
        "group_results": {"camera": {"large": "result"}},
        "cleanup_removed": [f"/tmp/work-{index}" for index in range(12)],
        "local_work_removed": [f"/tmp/work-{index}" for index in range(12)],
        "handoff_receipt": {
            "destination": "rclone",
            "returncode": 0,
            "stdout": "x" * 5000,
            "stderr": "",
            "location": "remote:path",
            "succeeded_at": "2026-01-01T00:01:00Z",
        },
        "handoff_metrics": {
            "registered_files": 0,
            "session_complete_elapsed_seconds": 0.25,
        },
        "handoff_adapter_state": {
            "files": {
                "camera/a.webm": {
                    "path": "camera/a.webm",
                    "bytes": 10,
                    "uploaded_bytes": 10,
                }
            }
        },
    }

    changed = server.cleanup_service.compact_terminal_job_state(job)

    assert changed is True
    assert job["encode_progress"]["files_total"] == 1
    assert job["encode_progress"]["files_encoded"] == 1
    assert job["cleanup_removed_count"] == 12
    assert len(job["cleanup_removed_sample"]) == 8
    assert "cleanup_removed" not in job
    assert job["local_work_removed_count"] == 12
    assert "local_work_removed" not in job
    assert "stdout" not in job["handoff_receipt"]
    assert len(job["handoff_receipt"]["stdout_tail"]) == 4000
    assert job["handoff_metrics"]["session_complete_elapsed_seconds"] == 0.25
    assert job["terminal_state_compacted_at"]
    for key in (
        "eager_archive",
        "gpu_payloads",
        "gpu_result",
        "gpu_results",
        "gpu_statuses",
        "group_results",
        "handoff_adapter_state",
    ):
        assert key not in job


def test_failed_job_diagnostic_preserves_pre_compaction_state(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    job = {
        "job_id": "job-1",
        "state": "failed",
        "phase": "gpu",
        "handoff": {"destination": "command", "options": {}},
        "error": "gpu job failed: bad encode",
        "eager_archive": {"files": {"camera/a.mp4": {"state": "failed"}}},
        "gpu_payloads": {"camera": {"input_dir": "/data/input"}},
        "gpu_results": {"camera": {"state": "failed"}},
    }

    diagnostic = server.diagnostic_service.create_job_diagnostic(
        job,
        reason="encoding_failed",
        error=server.domain_errors.EncodingFailed("gpu job failed: bad encode"),
    )
    server.cleanup_service.compact_terminal_job_state(job)

    assert diagnostic is not None
    stored = server.state_store.read_job_diagnostic("job-1")
    assert stored is not None
    bundle = Path(stored["path"])
    assert bundle.is_file()
    assert stored["bytes"] == bundle.stat().st_size
    assert stored["sha256"] == hashlib.sha256(bundle.read_bytes()).hexdigest()
    with tarfile.open(bundle, mode="r:gz") as archive:
        error_file = archive.extractfile("error.txt")
        state_file = archive.extractfile("job-state.json")
        assert error_file is not None
        assert state_file is not None
        assert error_file.read().decode("utf-8") == "gpu job failed: bad encode\n"
        full_state = json.loads(state_file.read())
    assert full_state["eager_archive"]["files"]["camera/a.mp4"]["state"] == "failed"
    assert full_state["gpu_payloads"]["camera"]["input_dir"] == "/data/input"
    assert "eager_archive" not in job
    assert diagnostic["reason"] == "encoding_failed"


def test_retention_uses_database_side_ttls_and_preserves_unsafe_jobs(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    for job_id, state, cleanup_completed in (
        ("expired-clean", "succeeded", True),
        ("expired-unclean", "failed", False),
        ("active-job", "running", False),
        ("diagnostic-job", "failed", True),
    ):
        job = {
            "job_id": job_id,
            "state": state,
            "phase": state,
            "created_at": "2025-01-01T00:00:00.000000Z",
            "finished_at": "2025-01-01T01:00:00.000000Z" if state != "running" else "",
        }
        if cleanup_completed:
            job["cleanup_completed_at"] = "2025-01-01T02:00:00.000000Z"
        server.state_store.save_job(job)
    diagnostic = server.diagnostic_service.create_job_diagnostic(
        server.state_store.load_job("diagnostic-job"),
        reason="job_failed",
    )
    assert diagnostic is not None
    diagnostic_record = server.state_store.read_job_diagnostic("diagnostic-job")
    assert diagnostic_record is not None
    diagnostic_path = Path(diagnostic_record["path"])
    with server.state_store.state_db() as conn:
        conn.execute(
            "UPDATE job_summaries SET updated_at = ? WHERE job_id IN (?, ?, ?)",
            (
                "2025-01-01T02:00:00.000000Z",
                "expired-clean",
                "expired-unclean",
                "active-job",
            ),
        )
        conn.execute(
            "UPDATE job_diagnostics SET created_at = ? WHERE job_id = ?",
            ("2025-01-01T02:00:00.000000Z", "diagnostic-job"),
        )
        conn.commit()
    monkeypatch.setattr(
        server.scheduling_service,
        "job_states",
        lambda: (_ for _ in ()).throw(AssertionError("retention must query SQLite directly")),
    )

    plan = server.retention_service.retention_plan()
    result = server.retention_service.apply_retention()

    assert plan["terminal_jobs"]["eligible"] == 1
    assert plan["terminal_jobs"]["sample_job_ids"] == ["expired-clean"]
    assert plan["job_diagnostics"]["eligible"] == 1
    assert plan["job_diagnostics"]["sample_job_ids"] == ["diagnostic-job"]
    assert result["removed"]["terminal_job_ids"] == ["expired-clean"]
    assert result["removed"]["job_diagnostic_ids"] == ["diagnostic-job"]
    assert server.state_store.read_state("job", "expired-clean") is None
    assert server.state_store.read_state("job", "expired-unclean") is not None
    assert server.state_store.read_state("job", "active-job") is not None
    assert server.state_store.read_state("job", "diagnostic-job") is not None
    assert server.state_store.read_job_diagnostic("diagnostic-job") is None
    assert not diagnostic_path.exists()


def test_manual_terminal_job_removal_requires_cleanup_and_removes_diagnostic(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    server.state_store.save_job(
        {
            "job_id": "job-1",
            "state": "failed",
            "phase": "failed",
            "error": "bad encode",
            "finished_at": "2026-08-01T00:00:00.000000Z",
        }
    )
    diagnostic = server.diagnostic_service.create_job_diagnostic(
        server.state_store.load_job("job-1"),
        reason="encoding_failed",
    )
    assert diagnostic is not None
    stored_diagnostic = server.state_store.read_job_diagnostic("job-1")
    assert stored_diagnostic is not None
    diagnostic_path = Path(stored_diagnostic["path"])

    with pytest.raises(server.ServiceError, match="local cleanup has not completed"):
        server.diagnostic_service.remove_terminal_job("job-1")

    job = server.state_store.load_job("job-1")
    job["cleanup_completed_at"] = utc_timestamp_now()
    server.state_store.save_job(job)
    receipt = server.diagnostic_service.remove_terminal_job("job-1")

    assert receipt == {
        "job_id": "job-1",
        "state": "failed",
        "diagnostic_removed": True,
        "removed": True,
    }
    assert server.state_store.read_state("job", "job-1") is None
    assert server.state_store.read_job_diagnostic("job-1") is None
    assert not diagnostic_path.exists()


def test_admin_can_list_download_and_remove_job_diagnostics(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MUNCHY_ADMIN_TOKEN", "admin-token")
    monkeypatch.setenv("MUNCHY_APPLICATION_AUTH_REQUIRED", "1")
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    server.state_store.save_job(
        {
            "job_id": "job-1",
            "state": "failed",
            "phase": "failed",
            "error": "bad encode",
        }
    )
    diagnostic = server.diagnostic_service.create_job_diagnostic(
        server.state_store.load_job("job-1"),
        reason="encoding_failed",
    )
    assert diagnostic is not None
    stored = server.state_store.read_job_diagnostic("job-1")
    assert stored is not None
    expected = Path(stored["path"]).read_bytes()
    client = TestClient(server.app)

    assert client.get("/v1/admin/job-diagnostics").status_code == 401
    headers = {"Authorization": "Bearer admin-token"}
    listed = client.get("/v1/admin/job-diagnostics?q=encoding", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["diagnostics"] == [diagnostic]
    metadata = client.get("/v1/admin/jobs/job-1/diagnostic", headers=headers)
    assert metadata.status_code == 200
    assert metadata.json() == diagnostic
    content = client.get("/v1/admin/jobs/job-1/diagnostic/content", headers=headers)
    assert content.status_code == 200
    assert content.headers["content-type"] == "application/gzip"
    assert content.headers["etag"] == f'"{diagnostic["sha256"]}"'
    assert content.content == expected
    removed = client.delete("/v1/admin/jobs/job-1/diagnostic", headers=headers)
    assert removed.status_code == 200
    assert removed.json() == {**diagnostic, "removed": True}
    assert server.state_store.read_job_diagnostic("job-1") is None


def test_prepare_shared_input_tree_links_uploaded_files_without_sha_rehash(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    data_path = server.upload_service.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"video")
    source_inode = data_path.stat().st_ino
    monkeypatch.setattr(
        server.upload_service,
        "file_sha256",
        lambda path: (_ for _ in ()).throw(AssertionError("sha256 should not run")),
    )
    upload = {
        "input_upload_id": "upload-1",
        "files": [
            {
                "path": "camera/a.mp4",
                "bytes": 5,
                "sha256": "0" * 64,
                "file_upload_id": "upload-a",
                "provenance": omitted_test_provenance(),
            }
        ],
    }

    root = server.upload_service.prepare_shared_input_tree(upload, {"camera"})

    linked = root / "camera" / "a.mp4"
    assert linked.read_bytes() == b"video"
    assert linked.stat().st_ino == source_inode
    assert not data_path.exists()
    marker = root / ".munchy-input-upload.json"
    metadata = json.loads(marker.read_text(encoding="utf-8"))
    assert metadata["input_upload_id"] == "upload-1"
    assert metadata["files"] == 1
    monkeypatch.setattr(
        server.upload_service,
        "materialize_upload_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("prepared shared input tree should be reused")
        ),
    )

    assert server.upload_service.prepare_shared_input_tree(upload, {"camera"}) == root


def test_sync_shared_input_tree_links_completed_files_incrementally(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    complete_path = server.upload_service.tusd_data_path("upload-a")
    partial_path = server.upload_service.tusd_data_path("upload-b")
    complete_path.parent.mkdir(parents=True, exist_ok=True)
    complete_path.write_bytes(b"video-a")
    partial_path.write_bytes(b"vid")
    upload = {
        "input_upload_id": "upload-1",
        "files": [
            {
                "path": "camera/a.mp4",
                "bytes": 7,
                "file_upload_id": "upload-a",
                "provenance": omitted_test_provenance(),
                "sha256": "a" * 64,
            },
            {
                "path": "camera/b.mp4",
                "bytes": 7,
                "file_upload_id": "upload-b",
                "provenance": omitted_test_provenance(),
                "sha256": "a" * 64,
            },
        ],
    }

    summary = server.upload_service.sync_shared_input_tree(upload, {"camera"})
    progress = server.upload_service.upload_group_progress(upload, {"camera"})
    root = server.upload_service.shared_input_upload_root("upload-1")

    assert summary["linked"] == 1
    assert (root / "camera" / "a.mp4").read_bytes() == b"video-a"
    assert not complete_path.exists()
    assert not (root / "camera" / "b.mp4").exists()
    assert partial_path.exists()
    assert progress["files_uploaded"] == 1
    assert progress["input_tree_files_ready"] == 1

    partial_path.write_bytes(b"video-b")
    summary = server.upload_service.sync_shared_input_tree(upload, {"camera"})
    progress = server.upload_service.upload_group_progress(upload, {"camera"})

    assert summary["linked"] == 2
    assert (root / "camera" / "b.mp4").read_bytes() == b"video-b"
    assert not partial_path.exists()
    assert progress["files_uploaded"] == 2
    assert progress["input_tree_files_ready"] == 2


def test_refresh_input_upload_does_not_sync_shared_input_tree(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    data_path = server.upload_service.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"video")
    upload = {
        "input_upload_id": "upload-1",
        "files": [
            {
                "path": "camera/a.mp4",
                "bytes": 5,
                "file_upload_id": "upload-a",
                "provenance": omitted_test_provenance(),
                "sha256": "a" * 64,
            },
        ],
    }
    server.upload_service.save_input_upload_raw(upload)

    monkeypatch.setattr(
        server.upload_service,
        "sync_shared_input_tree",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("status read must not materialize input tree")
        ),
    )

    status = server.upload_service.refresh_input_upload(
        server.upload_service.load_input_upload_raw("upload-1")
    )

    assert status["files_uploaded"] == 1
    assert not (
        server.upload_service.shared_input_upload_root("upload-1") / "camera" / "a.mp4"
    ).exists()


def test_eager_archive_upload_progress_does_not_report_shared_input_tree(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    server.upload_service.save_input_upload_raw(
        {
            "input_upload_id": "upload-1",
            "files": [
                {
                    "path": "camera/a.mp4",
                    "bytes": 7,
                    "file_upload_id": "upload-a",
                    "input_upload_id": "upload-1",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                }
            ],
        }
    )
    server.upload_service.tusd_data_path("upload-a").parent.mkdir(parents=True, exist_ok=True)
    server.upload_service.tusd_data_path("upload-a").write_bytes(b"video-a")
    monkeypatch.setattr(
        server.upload_service,
        "shared_input_tree_progress",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("eager archive progress should not scan shared input tree")
        ),
    )

    progress = server.processing_service.upload_progress_for_job(
        {
            "input_upload_id": "upload-1",
            "groups": {
                "camera": {
                    "output_mode": "video",
                    "tasks": ["archive_video"],
                }
            },
        }
    )

    assert progress is not None
    assert progress["files_uploaded"] == 1
    assert "input_tree_files_ready" not in progress
    assert "input_tree_bytes_ready" not in progress


def test_mixed_eager_archive_upload_progress_scopes_shared_input_tree_totals(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    server.upload_service.save_input_upload_raw(
        {
            "input_upload_id": "upload-1",
            "files": [
                {
                    "path": "camera/photo.jpg",
                    "bytes": 3,
                    "file_upload_id": "upload-photo",
                    "input_upload_id": "upload-1",
                    "resolved_group": "preserve",
                    "resolved_group_rel": "photo.jpg",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                },
                {
                    "path": "camera/video.mp4",
                    "bytes": 5,
                    "file_upload_id": "upload-video",
                    "input_upload_id": "upload-1",
                    "resolved_group": "video",
                    "resolved_group_rel": "video.mp4",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                },
            ],
        }
    )
    server.upload_service.tusd_data_path("upload-photo").parent.mkdir(parents=True, exist_ok=True)
    server.upload_service.tusd_data_path("upload-photo").write_bytes(b"jpg")
    server.upload_service.tusd_data_path("upload-video").write_bytes(b"video")

    progress = server.processing_service.upload_progress_for_job(
        {
            "input_upload_id": "upload-1",
            "groups": {
                "preserve": {"output_mode": "preserve", "tasks": []},
                "video": {"output_mode": "video", "tasks": ["archive_video"]},
            },
        }
    )

    assert progress is not None
    assert progress["files_total"] == 2
    assert progress["files_uploaded"] == 2
    assert progress["input_tree_files_ready"] == 0
    assert progress["input_tree_files_total"] == 1
    assert progress["input_tree_bytes_total"] == 3


def test_structured_unrouted_upload_progress_does_not_require_groups(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    server.upload_service.save_input_upload_raw(
        {
            "input_upload_id": "upload-1",
            "storage_hint": {"workflow_mode": "collection_archive", "structured_routing": True},
            "files": [
                {
                    "path": "front-door/clip.mp4",
                    "bytes": 7,
                    "file_upload_id": "upload-a",
                    "input_upload_id": "upload-1",
                    "structured_routing": True,
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                }
            ],
        }
    )
    server.upload_service.tusd_data_path("upload-a").parent.mkdir(parents=True, exist_ok=True)
    server.upload_service.tusd_data_path("upload-a").write_bytes(b"video-a")

    progress = server.processing_service.upload_progress_for_job(
        {
            "input_upload_id": "upload-1",
            "groups": {
                "video": {
                    "output_mode": "video",
                    "tasks": ["archive_video"],
                },
                "preserve": {"output_mode": "preserve", "tasks": []},
            },
        }
    )

    assert progress is not None
    assert progress["files_uploaded"] == 1
    assert progress["uploaded_bytes"] == 7
    assert "input_tree_files_ready" not in progress
    assert "input_tree_bytes_ready" not in progress


def test_wait_for_upload_groups_skips_configured_group_with_no_files(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    server.upload_service.save_input_upload_raw(
        {
            "input_upload_id": "upload-1",
            "state": "uploaded",
            "files": [
                {
                    "path": "front-door/clip.mp4",
                    "bytes": 7,
                    "file_upload_id": "upload-a",
                    "input_upload_id": "upload-1",
                    "resolved_group": "camera",
                    "resolved_group_rel": "front-door/clip.mp4",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                }
            ],
        }
    )
    server.upload_service.tusd_data_path("upload-a").parent.mkdir(parents=True, exist_ok=True)
    server.upload_service.tusd_data_path("upload-a").write_bytes(b"video-a")
    job = {
        "job_id": "job-1",
        "state": "running",
        "input_upload_id": "upload-1",
        "routing": {"routes": []},
    }
    server.state_store.save_job(job)

    upload = server.job_service.wait_for_upload_groups(
        job,
        "upload-1",
        {"preserve"},
        {"preserve": {"output_mode": "preserve", "tasks": []}},
    )

    assert upload["input_upload_id"] == "upload-1"
    assert job["upload_progress"]["files_total"] == 0
    assert job["upload_progress"]["files_uploaded"] == 0


def test_non_eager_upload_progress_still_reports_shared_input_tree(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    server.upload_service.save_input_upload_raw(
        {
            "input_upload_id": "upload-1",
            "files": [
                {
                    "path": "camera/a.mp4",
                    "bytes": 7,
                    "file_upload_id": "upload-a",
                    "input_upload_id": "upload-1",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                }
            ],
        }
    )
    root = server.upload_service.shared_input_upload_root("upload-1")
    (root / "camera").mkdir(parents=True)
    (root / "camera" / "a.mp4").write_bytes(b"video-a")

    progress = server.processing_service.upload_progress_for_job(
        {
            "input_upload_id": "upload-1",
            "groups": {
                "camera": {
                    "output_mode": "video",
                    "tasks": ["qcut_video"],
                }
            },
        }
    )

    assert progress is not None
    assert progress["input_tree_files_ready"] == 1
    assert progress["input_tree_bytes_ready"] == 7


def test_create_file_upload_does_not_sync_entire_shared_tree(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    upload_id = "upload-1"
    rel_path = "camera/a.mp4"
    target_path = server.upload_service.target_path_for(upload_id, rel_path)
    server.state_store.write_state(
        "input-upload",
        upload_id,
        {
            "input_upload_id": upload_id,
            "state": "uploading",
            "created_at": utc_timestamp_now(),
            "storage_hint": {
                "workflow_mode": "collection_archive",
                "handoff_destination": "riverhog",
                "groups": {
                    "camera": {
                        "output_mode": "video",
                        "tasks": ["archive_video"],
                    }
                },
            },
            "files": [
                {
                    "path": rel_path,
                    "bytes": 10,
                    "sha256": None,
                    "provenance": omitted_test_provenance(),
                    "target_path": target_path,
                    "input_upload_id": upload_id,
                    "file_upload_id": server.upload_service.tusd_upload_id_for_target_path(
                        target_path
                    ),
                    "upload_url": None,
                }
            ],
            "tusd_creation_url": server.runtime_config.TUSD_PUBLIC_BASE_URL,
        },
    )
    monkeypatch.setattr(
        server.upload_service,
        "sync_shared_input_tree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("full sync")),
    )

    class TrackingLock:
        active = False

        def __enter__(self):  # type: ignore[no-untyped-def]
            self.active = True

        def __exit__(self, *_args):  # type: ignore[no-untyped-def]
            self.active = False

    tracking_lock = TrackingLock()

    def fake_create_tusd_upload(target_path: str, _length: int) -> str:
        assert tracking_lock.active is False
        return f"{server.runtime_config.TUSD_PUBLIC_BASE_URL}/{target_path}"

    monkeypatch.setattr(server.execution_runtime, "state_lock", tracking_lock)
    monkeypatch.setattr(server.upload_service, "create_tusd_upload", fake_create_tusd_upload)

    response = server.job_service._create_or_resume_input_file_upload(upload_id, rel_path)

    assert response["offset"] == 0
    assert response["length"] == 10
    stored = server.state_store.read_state("input-upload", upload_id)
    assert stored is not None
    assert stored["files"][0]["upload_url"] == response["upload_url"]


def test_concurrent_file_upload_setup_creates_one_tusd_upload(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    upload_id = "upload-1"
    rel_path = "camera/a.mp4"
    target_path = server.upload_service.target_path_for(upload_id, rel_path)
    server.state_store.write_state(
        "input-upload",
        upload_id,
        {
            "input_upload_id": upload_id,
            "state": "uploading",
            "created_at": utc_timestamp_now(),
            "storage_hint": {
                "workflow_mode": "collection_archive",
                "handoff_destination": "riverhog",
                "groups": {
                    "camera": {
                        "output_mode": "video",
                        "tasks": ["archive_video"],
                    }
                },
            },
            "files": [
                {
                    "path": rel_path,
                    "bytes": 10,
                    "sha256": None,
                    "provenance": omitted_test_provenance(),
                    "target_path": target_path,
                    "input_upload_id": upload_id,
                    "file_upload_id": server.upload_service.tusd_upload_id_for_target_path(
                        target_path
                    ),
                    "upload_url": None,
                }
            ],
            "tusd_creation_url": server.runtime_config.TUSD_PUBLIC_BASE_URL,
        },
    )

    created_targets: list[str] = []
    responses: list[dict[str, object]] = []
    errors: list[BaseException] = []
    create_started = threading.Event()
    finish_create = threading.Event()
    caller_started = [threading.Event(), threading.Event()]
    created_targets_lock = threading.Lock()
    setup_lock = server.execution_runtime.input_file_upload_setup_lock(upload_id, rel_path)

    def fake_create_tusd_upload(target_path: str, _length: int) -> str:
        with created_targets_lock:
            created_targets.append(target_path)
        create_started.set()
        assert finish_create.wait(timeout=5)
        return f"{server.runtime_config.TUSD_PUBLIC_BASE_URL}/{target_path}"

    def fake_head_tusd_upload(_upload_url: str) -> int:
        return 0

    def call_upload(index: int) -> None:
        caller_started[index].set()
        try:
            with server.execution_runtime.input_file_upload_setup_lock(upload_id, rel_path):
                responses.append(
                    server.job_service._create_or_resume_input_file_upload(upload_id, rel_path)
                )
        except BaseException as exc:  # pragma: no cover - re-raised by the parent thread
            errors.append(exc)

    monkeypatch.setattr(server.upload_service, "create_tusd_upload", fake_create_tusd_upload)
    monkeypatch.setattr(server.upload_service, "head_tusd_upload", fake_head_tusd_upload)

    setup_lock.acquire()
    holding_setup_lock = True
    threads = [threading.Thread(target=call_upload, args=(index,)) for index in range(2)]
    try:
        for thread in threads:
            thread.start()
        assert caller_started[0].wait(timeout=5)
        assert caller_started[1].wait(timeout=5)
        assert not create_started.wait(timeout=0.25)
        setup_lock.release()
        holding_setup_lock = False
        assert create_started.wait(timeout=5)
        finish_create.set()
    finally:
        if holding_setup_lock:
            setup_lock.release()
        finish_create.set()
        for thread in threads:
            thread.join(timeout=5)

    assert errors == []
    assert len(responses) == 2
    assert created_targets == [target_path]
    stored = server.state_store.read_state("input-upload", upload_id)
    assert stored is not None
    assert (
        stored["files"][0]["upload_url"]
        == f"{server.runtime_config.TUSD_PUBLIC_BASE_URL}/{target_path}"
    )


def test_resume_file_upload_heads_tusd_outside_state_lock(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    upload_id = "upload-1"
    rel_path = "camera/a.mp4"
    target_path = server.upload_service.target_path_for(upload_id, rel_path)
    upload_url = f"{server.runtime_config.TUSD_PUBLIC_BASE_URL}/{target_path}"
    server.state_store.write_state(
        "input-upload",
        upload_id,
        {
            "input_upload_id": upload_id,
            "state": "uploading",
            "created_at": utc_timestamp_now(),
            "files": [
                {
                    "path": rel_path,
                    "bytes": 10,
                    "sha256": None,
                    "provenance": omitted_test_provenance(),
                    "target_path": target_path,
                    "input_upload_id": upload_id,
                    "file_upload_id": server.upload_service.tusd_upload_id_for_target_path(
                        target_path
                    ),
                    "upload_url": upload_url,
                }
            ],
            "storage_hint": {"source_bytes": 10},
            "tusd_creation_url": server.runtime_config.TUSD_PUBLIC_BASE_URL,
        },
    )

    class TrackingLock:
        active = False

        def __enter__(self):  # type: ignore[no-untyped-def]
            self.active = True

        def __exit__(self, *_args):  # type: ignore[no-untyped-def]
            self.active = False

    tracking_lock = TrackingLock()

    def fake_head_tusd_upload(_upload_url: str) -> int:
        assert tracking_lock.active is False
        return 4

    monkeypatch.setattr(server.execution_runtime, "state_lock", tracking_lock)
    monkeypatch.setattr(server.upload_service, "head_tusd_upload", fake_head_tusd_upload)

    response = server.job_service._create_or_resume_input_file_upload(upload_id, rel_path)

    assert response["offset"] == 4
    assert response["length"] == 10


def test_sync_shared_input_file_materializes_only_completed_file(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    upload_id = "upload-1"
    complete_path = server.upload_service.tusd_data_path("upload-a")
    partial_path = server.upload_service.tusd_data_path("upload-b")
    complete_path.parent.mkdir(parents=True, exist_ok=True)
    complete_path.write_bytes(b"video-a")
    partial_path.write_bytes(b"vid")
    server.state_store.write_state(
        "input-upload",
        upload_id,
        {
            "input_upload_id": upload_id,
            "state": "uploading",
            "created_at": utc_timestamp_now(),
            "storage_hint": {
                "workflow_mode": "collection_archive",
                "handoff_destination": "riverhog",
                "groups": {
                    "camera": {
                        "output_mode": "video",
                        "tasks": ["archive_video"],
                    }
                },
            },
            "files": [
                {
                    "path": "camera/a.mp4",
                    "bytes": 7,
                    "file_upload_id": "upload-a",
                    "target_path": server.upload_service.target_path_for(upload_id, "camera/a.mp4"),
                    "input_upload_id": upload_id,
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                },
                {
                    "path": "camera/b.mp4",
                    "bytes": 7,
                    "file_upload_id": "upload-b",
                    "target_path": server.upload_service.target_path_for(upload_id, "camera/b.mp4"),
                    "input_upload_id": upload_id,
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                },
            ],
        },
    )

    assert server.upload_service.sync_shared_input_file(upload_id, "camera/a.mp4") is True

    root = server.upload_service.shared_input_upload_root(upload_id)
    assert (root / "camera" / "a.mp4").read_bytes() == b"video-a"
    assert not complete_path.exists()
    assert not (root / "camera" / "b.mp4").exists()
    assert partial_path.exists()


def test_consume_input_upload_file_removes_only_that_shared_input_file(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    upload = server.upload_service.save_input_upload(
        {
            "input_upload_id": "upload-1",
            "files": [
                {
                    "path": "camera/a.mp4",
                    "bytes": 7,
                    "file_upload_id": "upload-a",
                    "input_upload_id": "upload-1",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                },
                {
                    "path": "camera/b.mp4",
                    "bytes": 7,
                    "file_upload_id": "upload-b",
                    "input_upload_id": "upload-1",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                },
            ],
        }
    )
    root = server.upload_service.shared_input_upload_root("upload-1")
    (root / "camera").mkdir(parents=True)
    (root / "camera" / "a.mp4").write_bytes(b"video-a")
    (root / "camera" / "b.mp4").write_bytes(b"video-b")

    upload = server.processing_service.consume_input_upload_files("upload-1", {"camera/a.mp4"})

    assert not (root / "camera" / "a.mp4").exists()
    assert (root / "camera" / "b.mp4").read_bytes() == b"video-b"
    by_path = {str(item["path"]): item for item in upload["files"]}
    assert by_path["camera/a.mp4"]["consumed_at"]
    assert by_path["camera/a.mp4"]["complete"] is True
    assert by_path["camera/b.mp4"]["complete"] is True


def test_cleanup_consumed_shared_input_files_removes_existing_consumed_files(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    upload = {
        "input_upload_id": "upload-1",
        "files": [
            {
                "path": "camera/a.mp4",
                "bytes": 7,
                "file_upload_id": "upload-a",
                "input_upload_id": "upload-1",
                "consumed_at": "2026-01-01T00:00:00Z",
                "provenance": omitted_test_provenance(),
                "sha256": "a" * 64,
            },
            {
                "path": "camera/b.mp4",
                "bytes": 7,
                "file_upload_id": "upload-b",
                "input_upload_id": "upload-1",
                "provenance": omitted_test_provenance(),
                "sha256": "a" * 64,
            },
        ],
    }
    root = server.upload_service.shared_input_upload_root("upload-1")
    (root / "camera").mkdir(parents=True)
    (root / "camera" / "a.mp4").write_bytes(b"video-a")
    (root / "camera" / "b.mp4").write_bytes(b"video-b")

    removed = server.upload_service.cleanup_consumed_shared_input_files(upload, {"camera"})
    progress = server.upload_service.shared_input_tree_progress(upload, {"camera"})

    assert removed == 1
    assert not (root / "camera" / "a.mp4").exists()
    assert (root / "camera" / "b.mp4").exists()
    assert progress["input_tree_files_ready"] == 2
    assert progress["input_tree_bytes_ready"] == 14


def test_link_or_copy_replaces_destination_atomically_on_copy_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    source = tmp_path / "source.mp4"
    dest = tmp_path / "dest" / "target.mp4"
    source.write_bytes(b"new-video")
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"old-video")
    copy_dests: list[Path] = []

    def fake_link(_source: Path, link_dest: Path) -> None:
        assert link_dest.name.startswith(".target.mp4.")
        assert link_dest.name.endswith(".part")
        raise OSError(errno.EXDEV, "cross-device")

    def fake_copy2(copy_source: Path, copy_dest: Path) -> Path:
        copy_dests.append(copy_dest)
        copy_dest.write_bytes(copy_source.read_bytes())
        return copy_dest

    monkeypatch.setattr(server.upload_service.os, "link", fake_link)
    monkeypatch.setattr(server.upload_service.shutil, "copy2", fake_copy2)

    server.upload_service.link_or_copy(source, dest)

    assert dest.read_bytes() == b"new-video"
    assert len(copy_dests) == 1
    assert copy_dests[0].name.startswith(".target.mp4.")
    assert copy_dests[0].name.endswith(".part")
    assert not copy_dests[0].exists()


def test_materialize_upload_file_reuses_existing_dest_when_tusd_data_is_gone(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    dest_root = tmp_path / "shared"
    dest = dest_root / "camera" / "a.mp4"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"video-a")
    file_state = {
        "path": "camera/a.mp4",
        "bytes": 7,
        "file_upload_id": "missing-upload",
        "provenance": omitted_test_provenance(),
        "sha256": "a" * 64,
    }

    server.upload_service.materialize_upload_file(file_state, dest_root)

    assert dest.read_bytes() == b"video-a"


def test_materialize_upload_file_retries_shared_source_when_tusd_disappears(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    upload_id = "upload-1"
    file_state = {
        "path": "camera/a.mp4",
        "bytes": 7,
        "file_upload_id": "upload-a",
        "input_upload_id": upload_id,
        "provenance": omitted_test_provenance(),
        "sha256": "a" * 64,
    }
    tusd_source = server.upload_service.tusd_data_path("upload-a")
    shared_source = server.upload_service.shared_input_file_path(file_state)
    assert shared_source is not None
    tusd_source.parent.mkdir(parents=True, exist_ok=True)
    tusd_source.write_bytes(b"video-a")
    dest_root = tmp_path / "batch"
    calls: list[Path] = []

    def fake_link_or_copy(source: Path, dest: Path) -> None:
        calls.append(source)
        if source == tusd_source:
            tusd_source.unlink()
            shared_source.parent.mkdir(parents=True, exist_ok=True)
            shared_source.write_bytes(b"video-a")
            raise RuntimeError("failed to copy missing tusd source")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(source.read_bytes())

    monkeypatch.setattr(server.upload_service, "link_or_copy", fake_link_or_copy)

    server.upload_service.materialize_upload_file(file_state, dest_root)

    assert calls == [tusd_source, shared_source]
    assert (dest_root / "camera" / "a.mp4").read_bytes() == b"video-a"


def test_shared_input_file_rejects_a_symlink_escape(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    root = server.upload_service.shared_input_upload_root("upload-1")
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "camera").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="escaped its configured root"):
        server.upload_service.shared_input_file_path(
            {
                "path": "camera/a.mp4",
                "input_upload_id": "upload-1",
            }
        )


def test_run_job_points_gpu_payload_at_shared_input_tree(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    data_path = server.upload_service.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"video")
    server.upload_service.save_input_upload(
        {
            "input_upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "storage_hint": {
                "workflow_mode": "review",
                "handoff_destination": "command",
                "output_mode": "video",
                "tasks": ["qcut_video"],
                "groups": {"camera": {"output_mode": "video", "tasks": ["qcut_video"]}},
            },
            "files": [
                {
                    "path": "camera/a.mp4",
                    "bytes": 5,
                    "file_upload_id": "upload-a",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                }
            ],
        }
    )
    server.state_store.save_job(
        {
            "job_id": "job-1",
            "state": "queued",
            "phase": "queued",
            "workflow_mode": "review",
            "template_id": "camera-review",
            "input_upload_id": "upload-1",
            "handoff": {"destination": "command", "options": {}},
            "review": {
                "route_id": "camera",
                "profile_id": "webm-q42",
            },
            "groups": {
                "camera": {
                    "output_mode": "video",
                    "tasks": ["qcut_video"],
                    "profile": "profile",
                    "max_parallel_encodes": 4,
                }
            },
        }
    )
    payloads: list[dict[str, object]] = []
    monkeypatch.setattr(server.media_service, "acquire_job_gpu", lambda job: "token")
    monkeypatch.setattr(server.media_service, "release_job_gpu", lambda job, token: None)
    monkeypatch.setattr(
        server.media_service, "start_gpu_job", lambda payload: payloads.append(payload)
    )
    monkeypatch.setattr(
        server.media_service,
        "wait_gpu_job",
        lambda gpu_job_id, *, gpu_payload, job: {"state": "succeeded"},
    )
    monkeypatch.setattr(
        server.external, "run_external_handoff", lambda *args, **kwargs: {"returncode": 0}
    )
    monkeypatch.setattr(server.event_service, "emit_job_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(server.event_service, "emit_job_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(server.job_service, "schedule_pending_jobs", lambda *args, **kwargs: [])

    server.job_service.run_job("job-1")

    assert len(payloads) == 1
    assert payloads[0]["max_parallel_encodes"] == 4
    assert str(payloads[0]["input_dir"]).startswith("/data/input-uploads/")
    assert str(payloads[0]["input_dir"]).endswith("/camera")
    assert "/jobs/job-1/input" not in str(payloads[0]["input_dir"])
    job = server.state_store.load_job("job-1")
    assert job["state"] == "succeeded"
    assert server.state_store.read_state("input-upload", "upload-1") is None
    assert not data_path.exists()
    assert not server.upload_service.shared_input_upload_root("upload-1").exists()


def test_successful_job_keeps_shared_input_upload_for_unfinished_sibling(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    data_path = server.upload_service.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"video")
    server.upload_service.save_input_upload(
        {
            "input_upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "storage_hint": {
                "workflow_mode": "review",
                "handoff_destination": "command",
                "output_mode": "video",
                "tasks": ["qcut_video"],
                "groups": {"camera": {"output_mode": "video", "tasks": ["qcut_video"]}},
            },
            "files": [
                {
                    "path": "camera/a.mp4",
                    "bytes": 5,
                    "file_upload_id": "upload-a",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                }
            ],
        }
    )
    common_job = {
        "state": "queued",
        "phase": "queued",
        "workflow_mode": "review",
        "input_upload_id": "upload-1",
        "handoff": {"destination": "command", "options": {}},
        "groups": {
            "camera": {
                "output_mode": "video",
                "tasks": ["qcut_video"],
                "profile": "profile",
            }
        },
    }
    server.state_store.save_job(
        {
            "job_id": "job-1",
            "template_id": "camera-review-q42",
            "review": {
                "route_id": "camera",
                "profile_id": "webm-q42",
            },
            **common_job,
        }
    )
    server.state_store.save_job(
        {
            "job_id": "job-2",
            "template_id": "camera-review-q44",
            "review": {
                "route_id": "camera",
                "profile_id": "webm-q44",
            },
            **common_job,
        }
    )
    monkeypatch.setattr(server.media_service, "acquire_job_gpu", lambda job: "token")
    monkeypatch.setattr(server.media_service, "release_job_gpu", lambda job, token: None)
    monkeypatch.setattr(server.media_service, "start_gpu_job", lambda payload: None)
    monkeypatch.setattr(
        server.media_service,
        "wait_gpu_job",
        lambda gpu_job_id, *, gpu_payload, job: {"state": "succeeded"},
    )
    monkeypatch.setattr(
        server.external, "run_external_handoff", lambda *args, **kwargs: {"returncode": 0}
    )
    monkeypatch.setattr(server.event_service, "emit_job_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(server.event_service, "emit_job_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(server.job_service, "schedule_pending_jobs", lambda *args, **kwargs: [])

    server.job_service.run_job("job-1")

    assert server.state_store.load_job("job-1")["state"] == "succeeded"
    assert server.state_store.load_job("job-2")["state"] == "queued"
    assert server.state_store.read_state("input-upload", "upload-1") is not None
    assert not data_path.exists()
    shared_root = server.upload_service.shared_input_upload_root("upload-1")
    assert (shared_root / "camera" / "a.mp4").read_bytes() == b"video"


def test_successful_riverhog_handoff_cleans_job_work_and_input_upload(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    data_path = server.upload_service.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"video")
    server.upload_service.save_input_upload(
        {
            "input_upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "storage_hint": {
                "workflow_mode": "collection_archive",
                "handoff_destination": "riverhog",
                "output_mode": "video",
                "tasks": ["archive_video", "qcut_video"],
                "groups": {
                    "camera": {
                        "output_mode": "video",
                        "tasks": ["archive_video", "qcut_video"],
                    }
                },
            },
            "files": [
                {
                    "path": "camera/a.mp4",
                    "bytes": 5,
                    "file_upload_id": "upload-a",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                }
            ],
        }
    )
    server.state_store.save_job(
        {
            "job_id": "job-1",
            "state": "queued",
            "phase": "queued",
            "workflow_mode": "collection_archive",
            "input_upload_id": "upload-1",
            "template_id": "camera-archive",
            "run_id": "20260101T000000Z",
            "groups": {
                "camera": {
                    "output_mode": "video",
                    "tasks": ["archive_video", "qcut_video"],
                    "profile": "profile",
                    "metadata_projection": {"enabled": False},
                }
            },
            "handoff": {"destination": "riverhog", "options": {}},
        }
    )
    monkeypatch.setattr(server.media_service, "acquire_job_gpu", lambda job: "token")
    monkeypatch.setattr(server.media_service, "release_job_gpu", lambda job, token: None)
    monkeypatch.setattr(server.media_service, "start_gpu_job", lambda payload: None)

    def wait_gpu_job(gpu_job_id, *, gpu_payload, job):  # type: ignore[no-untyped-def]
        (server.runtime_config.GPU_RUNTIME_DIR / "jobs" / "job-1" / "archive").mkdir(parents=True)
        return {"state": "succeeded"}

    monkeypatch.setattr(server.media_service, "wait_gpu_job", wait_gpu_job)
    monkeypatch.setattr(
        server.external, "run_external_handoff", lambda *args, **kwargs: {"returncode": 0}
    )
    monkeypatch.setattr(
        server.riverhog, "upload_to_riverhog", lambda *args, **kwargs: {"returncode": 0}
    )
    monkeypatch.setattr(server.riverhog.RiverhogHandoffAdapter, "refresh", lambda self, job: None)
    monkeypatch.setattr(
        server.riverhog.RiverhogHandoffAdapter,
        "safe_to_delete",
        lambda self, job: True,
    )
    monkeypatch.setattr(server.event_service, "emit_job_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(server.event_service, "emit_job_issue", lambda *args, **kwargs: None)

    server.job_service.run_job("job-1")

    job = server.state_store.load_job("job-1")
    assert job["state"] == "succeeded"
    assert job["handoff_receipt"] == {"returncode": 0}
    assert job["cleanup_completed_at"]
    assert "input-upload:upload-1" in job["cleanup_removed"]
    assert server.state_store.read_state("input-upload", "upload-1") is None
    assert not data_path.exists()
    assert not server.upload_service.shared_input_upload_root("upload-1").exists()
    assert not (server.runtime_config.GPU_RUNTIME_DIR / "jobs" / "job-1").exists()


def test_riverhog_handoff_uses_session_uploads_and_removes_local_artifacts(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MUNCHY_RIVERHOG_HANDOFF_ENABLED", "1")
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    archive_dir = server.runtime_config.GPU_RUNTIME_DIR / "jobs" / "job-1" / "archive"
    video = archive_dir / "camera" / "a.webm"
    sidecar = archive_dir / "camera" / "a.webm.source-artifacts.tar.zst"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    sidecar.write_bytes(b"meta")

    server.upload_service.save_input_upload_raw(
        {
            "input_upload_id": "upload-1",
            "state": "complete",
            "files": [
                {
                    "input_upload_id": "upload-1",
                    "file_upload_id": "file-1",
                    "path": "camera/a.mp4",
                    "bytes": 5,
                    "sha256": hashlib.sha256(b"input").hexdigest(),
                    "provenance": omitted_test_provenance(),
                }
            ],
        }
    )

    job = {
        "job_id": "job-1",
        "state": "running",
        "phase": "handoff",
        "workflow_mode": "collection_archive",
        "input_upload_id": "upload-1",
        "template_id": "camera-archive",
        "run_id": "20260101T000000Z",
        "groups": {"camera": {"output_mode": "video", "tasks": ["archive_video"]}},
        "handoff": {
            "destination": "riverhog",
            "options": {"tags": ["camera-archive"]},
        },
        "event_context": {"workflow": "desktop-archive"},
    }
    server.state_store.save_job(job)

    class FakeRiverhogApi:
        def __init__(self) -> None:
            self.registered: dict[str, dict[str, object]] = {}
            self.unit_content: bytes | None = None
            self.finalized = False

        def close(self) -> None:
            return

        def create_or_resume_collection_upload_session(
            self,
            idempotency_key: str,
            tags: list[str],
            *,
            ingest_source: str | None = None,
            archive_store: str | None = None,
            event_context: dict[str, object] | None = None,
            provenance_mode: str = "captured",
            provenance_omission_reason: str | None = None,
        ) -> dict[str, object]:
            assert idempotency_key == "job-1"
            assert tags == ["camera-archive"]
            assert ingest_source == str(archive_dir)
            assert event_context == {"workflow": "desktop-archive"}
            assert provenance_mode == "omitted"
            assert provenance_omission_reason
            return self.payload(state="open")

        def get_collection_upload_session(self, collection_id: int) -> dict[str, object]:
            assert collection_id == 42
            return self.payload(state="finalized" if self.finalized else "open")

        def register_collection_upload_session_files(
            self,
            collection_id: int,
            files: tuple[dict[str, object], ...],
        ) -> dict[str, object]:
            assert collection_id == 42
            assert [str(file["path"]) for file in files] == sorted(
                str(file["path"]) for file in files
            )
            self.registered.update((str(file["path"]), dict(file)) for file in files)
            return self.payload(state="open")

        def complete_collection_upload_session(
            self,
            collection_id: int,
            **_kwargs: object,
        ) -> dict[str, object]:
            assert collection_id == 42
            assert _kwargs["files_total"] == 2
            assert len(str(_kwargs["content_etag"])) == 64
            return self.payload(state="uploading")

        def list_collection_upload_session_volumes(self, collection_id: int) -> dict[str, object]:
            assert collection_id == 42
            return {
                "collection_id": 42,
                "volumes": [
                    {
                        "volume_id": "pack-000000000000",
                        "plan_sha256": "a" * 64,
                        "units": [
                            {
                                "unit": 0,
                                "payload_bytes": 9,
                                "plaintext_bytes": 9,
                                "sources": [
                                    {"path": path, "offset": 0, "bytes": int(file["bytes"])}
                                    for path, file in sorted(self.registered.items())
                                ],
                                "state": "committed" if self.finalized else "pending",
                            }
                        ],
                    }
                ],
            }

        def put_collection_upload_session_unit(
            self,
            collection_id: int,
            volume_id: str,
            unit: int,
            *,
            plan_sha256: str,
            content: bytes,
        ) -> dict[str, object]:
            assert (collection_id, volume_id, unit) == (42, "pack-000000000000", 0)
            assert plan_sha256 == "a" * 64
            self.unit_content = content
            self.finalized = True
            return {"state": "committed"}

        def payload(self, *, state: str) -> dict[str, object]:
            files: list[dict[str, object]] = [
                {
                    **payload,
                    "upload_state": "uploaded" if self.finalized else "pending",
                    "uploaded_bytes": int(payload["bytes"]) if self.finalized else 0,
                    "upload_state_expires_at": None,
                }
                for payload in self.registered.values()
            ]
            return {
                "collection_id": 42,
                "created_at": "2026-01-01T00:00:00Z",
                "tags": ["camera-archive"],
                "ingest_source": str(archive_dir),
                "archive_store": "primary",
                "state": state,
                "layout": {
                    "pack_source_bytes": 1024,
                    "pack_files": 100,
                    "pack_member_bytes": 1024,
                    "pack_part_plaintext_bytes": 1024,
                    "raw_volume_plaintext_bytes": 4096,
                    "raw_part_plaintext_bytes": 65536,
                },
                "files_total": len(files),
                "files_pending": 0 if self.finalized else len(files),
                "files_partial": 0,
                "files_uploaded": len(files) if self.finalized else 0,
                "bytes_total": sum(int(item["bytes"]) for item in files),
                "uploaded_bytes": sum(int(item["uploaded_bytes"]) for item in files),
                "archive_phase": "complete" if self.finalized else "planning",
                "archive_uploaded_bytes": 9 if self.finalized else 0,
                "archive_total_bytes": 9,
                "archive_uploaded_parts": 1 if self.finalized else 0,
                "archive_total_parts": 1,
                "files": files,
            }

    fake = FakeRiverhogApi()
    monkeypatch.setattr(server.riverhog, "ApiClient", lambda: fake)
    monkeypatch.setattr(server.event_service, "emit_job_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(server.event_service, "emit_job_issue", lambda *args, **kwargs: None)

    result = server.riverhog.upload_to_riverhog(job, archive_dir)

    assert result["destination"] == "riverhog"
    assert result["external_id"] == 42
    assert fake.finalized is True
    assert sorted(fake.registered) == [
        "camera/a.webm",
        "camera/a.webm.source-artifacts.tar.zst",
    ]
    assert fake.unit_content == b"videometa"
    assert not video.exists()
    assert not sidecar.exists()
    progress = server.riverhog.riverhog_handoff_progress(job)
    assert progress["files_uploaded"] == 2
    assert progress["bytes_total"] == 9
    metrics = job["handoff_metrics"]
    assert metrics["registration_processed_files"] == 2
    assert metrics["registered_files"] == 2
    assert metrics["registered_bytes"] == 9
    assert metrics["registration_before"] == {}
    assert metrics["registration_after"]["artifact_files_registered"] == 2
    assert metrics["session_complete_elapsed_seconds"] >= 0
    assert metrics["finished_at"]
    assert result["metrics"]["registered_files"] == 2
    assert result["metrics"]["session_complete_elapsed_seconds"] >= 0


def test_expected_riverhog_primary_files_total_counts_archive_outputs(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    upload = {
        "files": [
            {"path": "video/a.mp4", "bytes": 1},
            {"path": "video/b.mp4", "bytes": 1},
            {"path": "review/c.mp4", "bytes": 1},
        ],
    }
    groups = {
        "video": {"tasks": ["archive_video"]},
        "review": {"tasks": ["qcut_video"]},
    }

    assert server.riverhog.expected_riverhog_primary_files_total(upload, groups) == 2


def test_routed_riverhog_job_plans_path_only_primary_count(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    server.upload_service.save_input_upload_raw(
        {
            "input_upload_id": "upload-1",
            "state": "uploading",
            "storage_hint": {
                "workflow_mode": "collection_archive",
                "handoff_destination": "riverhog",
                "tasks": ["archive_video"],
                "structured_routing": True,
                "groups": {"video": {"output_mode": "video", "tasks": ["archive_video"]}},
            },
            "files": [
                {
                    "path": "camera/a.mp4",
                    "bytes": 1,
                    "file_upload_id": "a",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                },
                {
                    "path": "camera/b.mp4",
                    "bytes": 1,
                    "file_upload_id": "b",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                },
            ],
        }
    )

    job = server.job_service.create_job_state_from_request(
        server.domain_models.CreateJobRequest(
            input_upload_id="upload-1",
            run_id="20260101T000000Z",
            tasks=["archive_video"],
            groups={"video": {"output_mode": "video", "tasks": ["archive_video"]}},
            routing={
                "routes": [
                    {
                        "id": "camera-video",
                        "group": "video",
                        "when": {"path": {"suffix": ".mp4"}},
                    }
                ]
            },
            handoff={"destination": "riverhog"},
        )
    )

    assert job["handoff_expected_primary_files_total"] == 2


def test_save_job_preserves_newer_riverhog_upload_state(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    server.state_store.save_job(
        {
            "job_id": "job-1",
            "state": "running",
            "phase": "eager_archive",
            "handoff": {"destination": "riverhog", "options": {}},
            "handoff_adapter_state": {
                "state": "open",
                "updated_at": "2026-01-01T00:00:02Z",
                "files": {"camera/a.webm": {"state": "uploaded"}},
            },
        }
    )

    server.state_store.save_job(
        {
            "job_id": "job-1",
            "state": "running",
            "phase": "eager_archive:pipeline=3/3",
            "handoff": {"destination": "riverhog", "options": {}},
            "handoff_adapter_state": {
                "state": "open",
                "updated_at": "2026-01-01T00:00:01Z",
                "files": {},
            },
        }
    )

    stored = server.state_store.load_job("job-1")
    assert stored["phase"] == "eager_archive:pipeline=3/3"
    assert stored["handoff_adapter_state"]["files"] == {"camera/a.webm": {"state": "uploaded"}}


def test_handoff_retry_refreshes_persisted_job_state(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    monkeypatch.setattr(server.runtime_config, "HANDOFF_RETRY_INITIAL_SECONDS", 0.01)
    monkeypatch.setattr(server.runtime_config, "HANDOFF_RETRY_MAX_SECONDS", 0.01)

    stale_job = {
        "job_id": "job-1",
        "state": "running",
        "phase": "handoff_retrying",
        "handoff": {"destination": "riverhog", "options": {}},
        "handoff_adapter_state": {
            "state": "open",
            "updated_at": "2026-01-01T00:00:01Z",
            "files": {
                "camera/a.webm": {
                    "path": "camera/a.webm",
                    "bytes": 10,
                    "uploaded_bytes": 0,
                    "state": "registered",
                }
            },
        },
    }
    server.state_store.save_job(stale_job)
    attempts = 0

    def operation() -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        if not server.riverhog.all_riverhog_session_files_uploaded(stale_job):
            server.state_store.save_job(
                {
                    "job_id": "job-1",
                    "state": "running",
                    "phase": "handoff_retrying",
                    "handoff": {
                        "destination": "riverhog",
                        "options": {},
                    },
                    "handoff_adapter_state": {
                        "state": "open",
                        "updated_at": "2026-01-01T00:00:02Z",
                        "files": {
                            "camera/a.webm": {
                                "path": "camera/a.webm",
                                "bytes": 10,
                                "uploaded_bytes": 10,
                                "state": "deleted",
                            }
                        },
                    },
                }
            )
            raise RuntimeError("riverhog upload did not upload every registered file")
        return {"ok": True}

    result = server.handoff_service.retry_handoff_until_success(
        stale_job,
        result_key="handoff_receipt",
        phase="handoff",
        action="Riverhog handoff",
        component="riverhog_handoff",
        operation=operation,
    )

    assert result["ok"] is True
    assert attempts == 2
    assert stale_job["handoff_adapter_state"]["files"]["camera/a.webm"]["state"] == "deleted"


def test_save_job_compacts_gpu_results_before_persisting(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()

    server.state_store.save_job(
        {
            "job_id": "job-1",
            "state": "running",
            "phase": "eager_archive:pipeline=1/3",
            "eager_archive": {
                "files": {},
                "batches": {
                    "batch-1": {
                        "state": "succeeded",
                        "gpu_result": {
                            "job_id": "gpu-job-1",
                            "state": "succeeded",
                            "profile": "camera-archive",
                            "items": {
                                "archive_video": [
                                    {
                                        "source": "/data/in/a.mp4",
                                        "output": "/data/out/a.webm",
                                        "command": ["ffmpeg", "-i", "a.mp4"],
                                        "source_artifacts": {"audit": {"ok": True}},
                                    }
                                ]
                            },
                        },
                    }
                },
                "gpu_results": {
                    "batch-1": {
                        "job_id": "gpu-job-1",
                        "state": "succeeded",
                        "items": {"archive_video": [{"command": ["ffmpeg"]}]},
                    }
                },
            },
        }
    )

    stored = server.state_store.load_job("job-1")
    batch_result = stored["eager_archive"]["batches"]["batch-1"]["gpu_result"]
    stored_result = stored["eager_archive"]["gpu_results"]["batch-1"]
    assert batch_result == {
        "job_id": "gpu-job-1",
        "state": "succeeded",
        "profile": "camera-archive",
        "item_counts": {"archive_video": 1},
    }
    assert stored_result == {
        "job_id": "gpu-job-1",
        "state": "succeeded",
        "item_counts": {"archive_video": 1},
    }


def test_save_job_merges_newer_eager_archive_state(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    server.state_store.save_job(
        {
            "job_id": "job-1",
            "state": "running",
            "phase": "eager_archive:pipeline=2/3",
            "eager_archive": {
                "next_batch_number": 7,
                "files": {
                    "camera/a.mp4": {
                        "state": "encoded",
                        "encoded_at": "2026-01-01T00:00:03Z",
                        "output_bytes": 123,
                    }
                },
                "batches": {
                    "batch-1": {
                        "state": "succeeded",
                        "finished_at": "2026-01-01T00:00:03Z",
                    }
                },
                "gpu_results": {"batch-1": {"state": "succeeded"}},
            },
        }
    )

    server.state_store.save_job(
        {
            "job_id": "job-1",
            "state": "running",
            "phase": "riverhog-upload",
            "eager_archive": {
                "next_batch_number": 4,
                "files": {
                    "camera/a.mp4": {
                        "state": "encoding",
                        "started_at": "2026-01-01T00:00:01Z",
                    },
                    "camera/b.mp4": {
                        "state": "encoding",
                        "started_at": "2026-01-01T00:00:04Z",
                    },
                },
                "batches": {
                    "batch-1": {
                        "state": "running",
                        "started_at": "2026-01-01T00:00:01Z",
                    },
                    "batch-2": {
                        "state": "running",
                        "started_at": "2026-01-01T00:00:04Z",
                    },
                },
            },
        }
    )

    stored = server.state_store.load_job("job-1")
    eager = stored["eager_archive"]
    assert eager["next_batch_number"] == 7
    assert eager["files"]["camera/a.mp4"]["state"] == "encoded"
    assert eager["files"]["camera/a.mp4"]["output_bytes"] == 123
    assert eager["files"]["camera/b.mp4"]["state"] == "encoding"
    assert eager["batches"]["batch-1"]["state"] == "succeeded"
    assert eager["batches"]["batch-2"]["state"] == "running"
    assert eager["gpu_results"] == {"batch-1": {"state": "succeeded"}}


def test_save_job_does_not_resurrect_terminal_job_from_stale_worker_state(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    server.state_store.save_job(
        {
            "job_id": "job-1",
            "state": "canceled",
            "phase": "canceled",
            "finished_at": "2026-01-01T00:00:02Z",
        }
    )

    saved = server.state_store.save_job(
        {
            "job_id": "job-1",
            "state": "running",
            "phase": "cancel_requested",
            "cancel_requested": True,
            "updated_at": "2026-01-01T00:00:01Z",
        }
    )

    assert saved["state"] == "canceled"
    stored = server.state_store.load_job("job-1")
    assert stored["state"] == "canceled"
    assert stored["phase"] == "canceled"


def test_save_job_allows_explicit_resume_from_canceled_state(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    server.state_store.save_job(
        {
            "job_id": "job-1",
            "state": "canceled",
            "phase": "canceled",
            "finished_at": "2026-01-01T00:00:02Z",
        }
    )

    saved = server.state_store.save_job(
        {
            "job_id": "job-1",
            "state": "queued",
            "phase": "queued",
            "_allow_clear_cancel": True,
        }
    )

    assert saved["state"] == "queued"
    assert server.state_store.load_job("job-1")["state"] == "queued"


def test_handoff_progress_uses_expected_archive_output_count(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    monkeypatch.setattr(
        server.processing_service,
        "encode_progress_for_job",
        lambda job: {"files_total": 69},
    )

    files = {
        f"camera/{index}.webm": {
            "path": f"camera/{index}.webm",
            "bytes": 100,
            "uploaded_bytes": 100 if index < 6 else 0,
            "state": "uploaded" if index < 6 else "registered",
            "upload_state": "uploaded" if index < 6 else "partial",
        }
        for index in range(7)
    }
    job = {
        "job_id": "job-1",
        "handoff": {"destination": "riverhog", "options": {}},
        "handoff_expected_primary_files_total": 3636,
        "handoff_adapter_state": {
            "state": "open",
            "collection_id": 42,
            "files": files,
        },
    }

    progress = server.riverhog.riverhog_handoff_progress(job)

    assert progress["registered_files_total"] == 7
    assert progress["expected_primary_files_total"] == 3636
    assert progress["files_total"] == 3636
    assert progress["files_uploaded"] == 6
    assert progress["primary_files_total"] == 3636
    assert progress["primary_files_uploaded"] == 6
    assert progress["artifact_files_known"] == 7
    assert progress["artifact_files_uploaded"] == 6
    assert progress["percent_files"] == 0.17
    assert progress["percent_primary_files"] == 0.17


def test_sync_riverhog_session_uses_durable_finalized_upload_status(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    job = {
        "job_id": "job-1",
        "handoff": {"destination": "riverhog", "options": {}},
        "handoff_adapter_state": {
            "state": "archiving",
            "collection_id": 42,
            "files": {},
        },
    }

    class FakeApi:
        def get_collection_upload_session(self, collection_id: int) -> dict[str, object]:
            assert collection_id == 42
            return {
                "collection_id": collection_id,
                "state": "finalized",
                "files_total": 7,
                "files_uploaded": 7,
                "bytes_total": 1234,
                "uploaded_bytes": 1234,
                "archive_store": "deep",
                "archive_phase": "completed",
                "archive_uploaded_bytes": 567,
                "archive_total_bytes": 567,
            }

    payload = server.riverhog.sync_riverhog_session_from_remote(job, FakeApi())  # type: ignore[arg-type]
    progress = server.riverhog.riverhog_handoff_progress(job)

    assert payload is not None
    assert payload["state"] == "finalized"
    assert progress["state"] == "finalized"
    assert progress["safe_to_delete"] is True
    assert progress["destination_bytes_total"] == 1234
    assert progress["archive_uploaded_bytes"] == 567


def test_refresh_riverhog_session_uses_compacted_upload_result(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    job = {
        "job_id": "job-1",
        "state": "succeeded",
        "handoff": {"destination": "riverhog", "options": {}},
        "handoff_receipt": {
            "destination": "riverhog",
            "external_id": 42,
        },
    }
    server.state_store.save_job(job)

    class FakeApi:
        def get_collection_upload_session(self, collection_id: int) -> dict[str, object]:
            assert collection_id == 42
            return {
                "collection_id": collection_id,
                "state": "archiving",
                "files_total": 14,
                "files_uploaded": 14,
                "bytes_total": 1234,
                "uploaded_bytes": 1234,
                "archive_phase": "uploading",
                "archive_uploaded_bytes": 256,
                "archive_total_bytes": 1024,
                "archive_uploaded_parts": 1,
                "archive_total_parts": 4,
            }

        def close(self) -> None:
            pass

    monkeypatch.setattr(server.riverhog, "ApiClient", FakeApi)

    job = server.state_store.load_job("job-1")
    server.riverhog.refresh_riverhog_session_from_remote(job)
    refreshed = server.processing_service.job_response(job)
    progress = refreshed["handoff_progress"]

    assert progress["external_id"] == 42
    assert progress["state"] == "archiving"
    assert progress["safe_to_delete"] is False
    archive_stage = progress["stages"][1]
    assert archive_stage["state"] == "uploading"
    assert archive_stage["bytes_done"] == 256
    assert archive_stage["bytes_total"] == 1024
    assert archive_stage["items_done"] == 1
    assert archive_stage["items_total"] == 4


def test_cancel_riverhog_upload_session_cancels_open_session(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MUNCHY_RIVERHOG_HANDOFF_ENABLED", "1")
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()

    job = {
        "job_id": "job-1",
        "state": "running",
        "phase": "riverhog_upload",
        "handoff": {"destination": "riverhog", "options": {}},
        "handoff_adapter_state": {
            "state": "open",
            "collection_id": 42,
            "files": {},
        },
    }
    server.state_store.save_job(job)

    class FakeRiverhogApi:
        def __init__(self) -> None:
            self.canceled: list[str] = []

        def close(self) -> None:
            return

        def cancel_collection_upload_session(self, collection_id: int) -> dict[str, object]:
            self.canceled.append(collection_id)
            return {
                "collection_id": collection_id,
                "state": "canceled",
                "files_total": 0,
                "files_uploaded": 0,
                "bytes_total": 0,
                "uploaded_bytes": 0,
                "missing_bytes": 0,
                "files": [],
            }

    fake = FakeRiverhogApi()
    monkeypatch.setattr(server.riverhog, "ApiClient", lambda: fake)

    server.riverhog.cancel_riverhog_upload_session(job, reason="test")

    assert fake.canceled == [42]
    stored = server.state_store.load_job("job-1")
    assert stored["handoff_adapter_state"]["state"] == "canceled"
    assert stored["handoff_adapter_state"]["canceled_at"]
    assert stored["handoff_adapter_state"]["cancel_reason"] == "test"


def test_failed_job_default_policy_preserves_uploaded_riverhog_session(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    server.upload_service.save_input_upload_raw(
        {
            "input_upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "storage_hint": {
                "workflow_mode": "collection_archive",
                "handoff_destination": "riverhog",
                "output_mode": "video",
                "tasks": ["archive_video"],
                "groups": {},
            },
            "files": [],
        }
    )
    server.state_store.save_job(
        {
            "job_id": "job-1",
            "state": "queued",
            "phase": "queued",
            "workflow_mode": "collection_archive",
            "input_upload_id": "upload-1",
            "handoff": {
                "destination": "riverhog",
                "options": {},
                "on_failure": "preserve_for_resume",
            },
            "handoff_adapter_state": {
                "collection_id": 42,
                "state": "open",
                "files": {
                    "camera/a.webm": {
                        "path": "camera/a.webm",
                        "bytes": 7,
                        "uploaded_bytes": 7,
                        "state": "uploaded",
                    }
                },
            },
        }
    )
    monkeypatch.setattr(server.processing_service, "ensure_job_groups", lambda _job, _upload: {})
    monkeypatch.setattr(
        server.routing_service,
        "write_metadata_projection_sidecars",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("projection failed")),
    )
    monkeypatch.setattr(
        server.riverhog,
        "cancel_riverhog_upload_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("default policy should preserve the uploaded session")
        ),
    )
    monkeypatch.setattr(server.event_service, "emit_job_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(server.job_service, "schedule_pending_jobs", lambda *args, **kwargs: None)

    server.job_service.run_job("job-1")

    stored = server.state_store.load_job("job-1")
    assert stored["state"] == "failed"
    assert stored["handoff_adapter_state"]["preserved_after_failure_at"]


def test_failed_job_cancel_policy_cancels_uploaded_riverhog_session(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    server.upload_service.save_input_upload_raw(
        {
            "input_upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "storage_hint": {
                "workflow_mode": "collection_archive",
                "handoff_destination": "riverhog",
                "output_mode": "video",
                "tasks": ["archive_video"],
                "groups": {},
            },
            "files": [],
        }
    )
    server.state_store.save_job(
        {
            "job_id": "job-1",
            "state": "queued",
            "phase": "queued",
            "workflow_mode": "collection_archive",
            "input_upload_id": "upload-1",
            "handoff": {
                "destination": "riverhog",
                "options": {},
                "on_failure": "cancel",
            },
            "handoff_adapter_state": {
                "collection_id": 42,
                "state": "open",
                "files": {
                    "camera/a.webm": {
                        "path": "camera/a.webm",
                        "bytes": 7,
                        "uploaded_bytes": 7,
                        "state": "uploaded",
                    }
                },
            },
        }
    )
    canceled: list[str] = []
    monkeypatch.setattr(server.processing_service, "ensure_job_groups", lambda _job, _upload: {})
    monkeypatch.setattr(
        server.routing_service,
        "write_metadata_projection_sidecars",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("projection failed")),
    )
    monkeypatch.setattr(
        server.riverhog,
        "cancel_riverhog_upload_session",
        lambda _job, *, reason: canceled.append(reason),
    )
    monkeypatch.setattr(server.event_service, "emit_job_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(server.job_service, "schedule_pending_jobs", lambda *args, **kwargs: None)

    server.job_service.run_job("job-1")

    stored = server.state_store.load_job("job-1")
    assert stored["state"] == "failed"
    assert canceled == ["job_failed"]
    assert "preserved_after_failure_at" not in stored["handoff_adapter_state"]


def test_cancel_riverhog_upload_session_treats_missing_session_as_clean(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MUNCHY_RIVERHOG_HANDOFF_ENABLED", "1")
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()

    job = server.state_store.save_job(
        {
            "job_id": "job-1",
            "state": "running",
            "phase": "cancel_requested",
            "template_id": "camera-archive",
            "run_id": "20260101T000000Z",
            "handoff": {"destination": "riverhog", "options": {}},
            "handoff_adapter_state": {"collection_id": 42, "state": "open"},
        }
    )

    class FakeRiverhogApi:
        def close(self) -> None:
            return

        def cancel_collection_upload_session(self, collection_id: int) -> dict[str, object]:
            raise NotFound("missing")

    monkeypatch.setattr(server.riverhog, "ApiClient", FakeRiverhogApi)

    server.riverhog.cancel_riverhog_upload_session(job, reason="test")

    stored = server.state_store.load_job("job-1")
    assert stored["handoff_adapter_state"]["state"] == "canceled"
    assert stored["handoff_adapter_state"]["remote_state"] == "absent"
    assert stored["handoff_adapter_state"]["cancel_not_found"] is True
    assert stored["handoff_adapter_state"]["canceled_at"]


def test_repeated_cleanup_updates_empty_cleanup_summary(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    work_dir = server.runtime_config.GPU_RUNTIME_DIR / "jobs" / "job-1"
    work_dir.mkdir(parents=True)
    (work_dir / "scratch.txt").write_text("scratch", encoding="utf-8")
    job = {
        "job_id": "job-1",
        "state": "canceled",
        "phase": "canceled",
        "handoff": {"destination": "command", "options": {}},
        "cleanup_completed_at": "2026-01-01T00:00:00Z",
        "cleanup_removed": [],
        "cleanup_removed_count": 0,
    }

    server.cleanup_service.cleanup_terminal_job(job)
    server.cleanup_service.compact_terminal_job_state(job)

    assert job["cleanup_removed_count"] == 1
    assert job["cleanup_removed"] == [str(work_dir)]
    assert job["local_work_removed_count"] == 1


def test_encoding_failure_cleans_job_work_and_input_upload(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    data_path = server.upload_service.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"video")
    server.upload_service.save_input_upload(
        {
            "input_upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "storage_hint": {
                "workflow_mode": "review",
                "handoff_destination": "rclone",
                "output_mode": "video",
                "tasks": ["qcut_video"],
                "groups": {"camera": {"output_mode": "video", "tasks": ["qcut_video"]}},
            },
            "files": [
                {
                    "path": "camera/a.mp4",
                    "bytes": 5,
                    "file_upload_id": "upload-a",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                }
            ],
        }
    )
    server.state_store.save_job(
        {
            "job_id": "job-1",
            "state": "queued",
            "phase": "queued",
            "workflow_mode": "review",
            "template_id": "camera-review",
            "input_upload_id": "upload-1",
            "handoff": {"destination": "command", "options": {}},
            "review": {
                "route_id": "camera",
                "profile_id": "webm-q42",
            },
            "groups": {
                "camera": {
                    "output_mode": "video",
                    "tasks": ["qcut_video"],
                    "profile": "profile",
                }
            },
        }
    )
    monkeypatch.setattr(server.media_service, "acquire_job_gpu", lambda job: "token")
    monkeypatch.setattr(server.media_service, "release_job_gpu", lambda job, token: None)
    monkeypatch.setattr(server.media_service, "start_gpu_job", lambda payload: None)
    monkeypatch.setattr(
        server.media_service,
        "wait_gpu_job",
        lambda gpu_job_id, *, gpu_payload, job: (_ for _ in ()).throw(
            server.domain_errors.EncodingFailed("gpu job failed: bad encode")
        ),
    )
    monkeypatch.setattr(server.event_service, "emit_job_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(server.event_service, "emit_job_issue", lambda *args, **kwargs: None)

    server.job_service.run_job("job-1")

    job = server.state_store.load_job("job-1")
    assert job["state"] == "failed"
    assert job["error"] == "gpu job failed: bad encode"
    assert job["cleanup_completed_at"]
    assert "input-upload:upload-1" in job["cleanup_removed"]
    assert server.state_store.read_state("input-upload", "upload-1") is None
    assert not data_path.exists()
    assert not server.upload_service.shared_input_upload_root("upload-1").exists()
    assert not (server.runtime_config.GPU_RUNTIME_DIR / "jobs" / "job-1").exists()


def test_run_job_reuses_stored_shared_review_plan(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    data_path = server.upload_service.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"video")
    plan = {
        "kind": "munchy.qcut-plan",
        "version": 1,
        "clips": [{"index": 1, "source": "/data/input-uploads/upload/camera/a.mp4"}],
        "files": [{"path": "/data/input-uploads/upload/camera/a.mp4"}],
    }
    server.upload_service.store_shared_review_plan("upload-1", "camera", "qcut_video", plan)
    server.upload_service.save_input_upload(
        {
            "input_upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "storage_hint": {
                "workflow_mode": "review",
                "handoff_destination": "command",
                "output_mode": "video",
                "tasks": ["qcut_video"],
                "groups": {"camera": {"output_mode": "video", "tasks": ["qcut_video"]}},
            },
            "files": [
                {
                    "path": "camera/a.mp4",
                    "bytes": 5,
                    "file_upload_id": "upload-a",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                }
            ],
        }
    )
    server.state_store.save_job(
        {
            "job_id": "job-2",
            "state": "queued",
            "phase": "queued",
            "workflow_mode": "review",
            "template_id": "camera-review",
            "input_upload_id": "upload-1",
            "handoff": {"destination": "command", "options": {}},
            "review": {
                "route_id": "camera",
                "profile_id": "webm-q43",
                "clip_plan": {
                    "target_seconds": 90,
                    "min_seconds": 4,
                    "max_seconds": 7,
                },
            },
            "groups": {
                "camera": {
                    "output_mode": "video",
                    "tasks": ["qcut_video"],
                    "profile": "profile",
                }
            },
        }
    )
    payloads: list[dict[str, object]] = []
    monkeypatch.setattr(server.media_service, "acquire_job_gpu", lambda job: "token")
    monkeypatch.setattr(server.media_service, "release_job_gpu", lambda job, token: None)
    monkeypatch.setattr(
        server.media_service, "start_gpu_job", lambda payload: payloads.append(payload)
    )
    monkeypatch.setattr(
        server.media_service,
        "wait_gpu_job",
        lambda gpu_job_id, *, gpu_payload, job: {"state": "succeeded"},
    )
    monkeypatch.setattr(
        server.external, "run_external_handoff", lambda *args, **kwargs: {"returncode": 0}
    )
    monkeypatch.setattr(server.event_service, "emit_job_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(server.event_service, "emit_job_issue", lambda *args, **kwargs: None)

    server.job_service.run_job("job-2")

    assert payloads[0]["review_plans"]["qcut_video"]["kind"] == "munchy.qcut-plan"  # type: ignore[index]
    assert payloads[0]["review_plans"]["qcut_video"]["shared_plan"]["input_upload_id"] == "upload-1"  # type: ignore[index]
    assert payloads[0]["review_clip_plan"] == {
        "target_seconds": 90,
        "min_seconds": 4,
        "max_seconds": 7,
    }


def test_run_job_runs_review_sweep_as_one_job(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    data_path = server.upload_service.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"video")
    server.upload_service.save_input_upload(
        {
            "input_upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "storage_hint": {
                "workflow_mode": "review",
                "handoff_destination": "rclone",
                "output_mode": "video",
                "tasks": ["qcut_video"],
                "groups": {
                    "video": {"output_mode": "video", "tasks": ["qcut_video"]},
                    "photo": {"output_mode": "preserve", "tasks": []},
                },
                "structured_routing": True,
            },
            "files": [
                {
                    "path": "DCIM/a.mp4",
                    "bytes": 5,
                    "file_upload_id": "upload-a",
                    "resolved_group": "video",
                    "resolved_group_rel": "a.mp4",
                    "route_id": "video-4k",
                    "route_action": "upload",
                    "routed_at": "2026-01-01T00:00:00Z",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                }
            ],
        }
    )
    server.state_store.save_job(
        {
            "job_id": "job-1",
            "state": "queued",
            "phase": "queued",
            "workflow_mode": "review",
            "template_id": "camera-review-sweep",
            "input_upload_id": "upload-1",
            "handoff": {
                "destination": "rclone",
                "options": {
                    "location": "review-remote:reviews/{route_id}/{profile_id}",
                },
            },
            "review": {
                "sweep": {"quality": [24, 28], "route_ids": ["video-4k"]},
            },
            "groups": {
                "video": {
                    "output_mode": "video",
                    "tasks": ["qcut_video"],
                    "profile": "profile",
                    "encode_profile": {
                        "schema_version": 1,
                        "target": "munchy-av1-nvenc",
                        "name": "base",
                        "archive": {
                            "codec": "av1_nvenc",
                            "container": "webm",
                            "quality": 40,
                        },
                    },
                },
                "photo": {"output_mode": "preserve", "tasks": []},
            },
        }
    )
    payloads: list[dict[str, object]] = []
    uploads: list[tuple[str, str, bool]] = []
    events: list[str] = []
    monkeypatch.setattr(server.media_service, "acquire_job_gpu", lambda job: "token")
    monkeypatch.setattr(server.media_service, "release_job_gpu", lambda job, token: None)
    monkeypatch.setattr(
        server.media_service, "start_gpu_job", lambda payload: payloads.append(payload)
    )

    def fake_wait_gpu_job(gpu_job_id, *, gpu_payload, job):  # type: ignore[no-untyped-def]
        return {
            "state": "succeeded",
            "items": {
                "qcut_video": {
                    "plan": {
                        "kind": "munchy.qcut-plan",
                        "files": [{"path": gpu_payload["input_dir"]}],
                    }
                }
            },
        }

    def fake_run_external_handoff(job, source_dir, **kwargs):  # type: ignore[no-untyped-def]
        review = job["review"]
        template_context = kwargs["template_context"]
        assert "route_id" not in review
        assert "profile_id" not in review
        uploads.append(
            (
                template_context["route_id"],
                template_context["profile_id"],
                bool(kwargs.get("emit_event", True)),
            )
        )
        return {"status": "uploaded", "source": str(source_dir)}

    monkeypatch.setattr(server.media_service, "wait_gpu_job", fake_wait_gpu_job)
    monkeypatch.setattr(server.external, "run_external_handoff", fake_run_external_handoff)
    monkeypatch.setattr(
        server.event_service,
        "emit_job_event",
        lambda job, event, message, **kwargs: events.append(event),
    )
    monkeypatch.setattr(server.event_service, "emit_job_issue", lambda *args, **kwargs: None)

    server.job_service.run_job("job-1")

    assert len(payloads) == 2
    assert [payload["encode_profile"]["archive"]["quality"] for payload in payloads] == [24, 28]
    assert "review_plans" not in payloads[0]
    second_plan = payloads[1]["review_plans"]["qcut_video"]  # type: ignore[index]
    assert second_plan["kind"] == "munchy.qcut-plan"
    assert uploads == [
        ("video-4k", "q24", False),
        ("video-4k", "q28", False),
    ]
    assert events.count("review.handoff") == 1
    assert events.count("job.succeeded") == 1
    job = server.state_store.load_job("job-1")
    assert job["state"] == "succeeded"
    assert "route_id" not in job["review"]
    assert "profile_id" not in job["review"]
    assert job["review_sweep_result"]["variants_total"] == 2
    assert job["review_sweep_result"]["variants_completed"] == 2
    assert not any(key.startswith("review_sweep_upload_") for key in job)
    assert server.state_store.read_state("input-upload", "upload-1") is None


def test_preflight_issue_event_error_keeps_truncated_filename_at_end(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    filename = "example-camera-" + ("very-long-" * 12) + "clip.mp4"

    error = server.preflight_issue_event_error(
        path=f"camera-video/2026/06/06/{filename}",
        issue_message=(
            "ffprobe failed because the MP4 atom table points past the end of the "
            "available local file"
        ),
    )

    assert len(error) <= 120
    assert error.startswith("ffprobe failed because")
    assert error.endswith("clip.mp4)")
    assert " (..." in error


def test_ready_eager_files_skips_claimed_encoding_files(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    (server.runtime_config.TUSD_DIR / "upload-b").write_bytes(b"b")
    job = {
        "job_id": "job-1",
        "eager_archive": {
            "files": {
                "camera/a.mp4": {
                    "state": "encoding",
                    "batch_id": "batch-1",
                },
                "camera/c.mp4": {
                    "state": "failed",
                    "batch_id": "batch-2",
                },
            },
            "batches": {},
            "next_batch_number": 1,
        },
    }
    upload = {
        "input_upload_id": "upload-1",
        "files": [
            {
                "path": "camera/a.mp4",
                "bytes": 1,
                "file_upload_id": "upload-a",
                "provenance": omitted_test_provenance(),
                "sha256": "a" * 64,
            },
            {
                "path": "camera/b.mp4",
                "bytes": 1,
                "file_upload_id": "upload-b",
                "provenance": omitted_test_provenance(),
                "sha256": "a" * 64,
            },
            {
                "path": "camera/c.mp4",
                "bytes": 1,
                "file_upload_id": "upload-c",
                "provenance": omitted_test_provenance(),
                "sha256": "a" * 64,
            },
        ],
    }

    ready = server.processing_service.ready_eager_files(
        job,
        upload,
        {"camera": {}},
        {"camera"},
        tmp_path / "archive",
        limit=32,
    )

    assert ready is not None
    group_name, files = ready
    assert group_name == "camera"
    assert [file_state["path"] for file_state in files] == ["camera/b.mp4"]


def test_eager_group_pipeline_capacity_respects_group_limit(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    monkeypatch.setattr(server.runtime_config, "EAGER_ARCHIVE_PIPELINE_BATCHES", 3)
    job = {
        "job_id": "job-1",
        "eager_archive": {
            "batches": {
                "batch-1": {
                    "state": "running",
                    "executor": "gpu",
                    "group": "camera",
                    "started_at": "2026-06-05T00:00:00Z",
                }
            }
        },
    }

    assert (
        server.processing_service.eager_group_has_pipeline_capacity(
            job,
            "camera",
            {"eager_pipeline_batches": 1},
        )
        is False
    )
    assert (
        server.processing_service.eager_group_has_pipeline_capacity(
            job,
            "front-door",
            {"eager_pipeline_batches": 1},
        )
        is True
    )
    assert (
        server.processing_service.eager_group_has_pipeline_capacity(
            job,
            "camera",
            {"eager_pipeline_batches": 2},
        )
        is True
    )


def test_eager_group_pipeline_capacity_counts_mixed_executors_globally(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    monkeypatch.setattr(server.runtime_config, "EAGER_ARCHIVE_PIPELINE_BATCHES", 3)
    job = {
        "job_id": "job-1",
        "eager_archive": {
            "batches": {
                "batch-1": {"state": "running", "executor": "gpu", "group": "camera"},
                "batch-2": {"state": "running", "executor": "gpu", "group": "camera"},
                "batch-3": {
                    "state": "running",
                    "executor": "local_audio",
                    "group": "voice",
                },
            }
        },
    }

    assert (
        server.processing_service.eager_group_has_pipeline_capacity(
            job,
            "front-door",
            {"eager_pipeline_batches": 3},
        )
        is False
    )


def test_eager_archive_pipeline_phase_uses_single_group_limit(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    monkeypatch.setattr(server.runtime_config, "EAGER_ARCHIVE_PIPELINE_BATCHES", 3)
    job = {
        "job_id": "job-1",
        "eager_archive": {
            "batches": {
                "batch-1": {"state": "running", "executor": "gpu", "group": "camera"},
            }
        },
    }

    assert (
        server.processing_service.eager_archive_pipeline_phase(
            job,
            {"camera": {"eager_pipeline_batches": 1}},
        )
        == "eager_archive:camera:pipeline=1/1"
    )


def test_eager_archive_pipeline_phase_uses_global_limit_for_mixed_groups(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    monkeypatch.setattr(server.runtime_config, "EAGER_ARCHIVE_PIPELINE_BATCHES", 3)
    job = {
        "job_id": "job-1",
        "eager_archive": {
            "batches": {
                "batch-1": {"state": "running", "executor": "gpu", "group": "camera"},
                "batch-2": {"state": "running", "executor": "local_audio", "group": "voice"},
            }
        },
    }

    assert server.processing_service.eager_archive_pipeline_phase(
        job, {"camera": {}, "voice": {}}
    ) == ("eager_archive:pipeline=2/3")


def test_ready_eager_files_limits_audio_batches_to_worker_count(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    monkeypatch.setattr(server.runtime_config, "AUDIO_ARCHIVE_MAX_PARALLEL", 2)
    for upload_id in ("upload-a", "upload-b", "upload-c"):
        (server.runtime_config.TUSD_DIR / upload_id).write_bytes(b"x")
    job = {"job_id": "job-1"}
    upload = {
        "input_upload_id": "upload-1",
        "files": [
            {
                "path": "voice/a.mp3",
                "bytes": 1,
                "file_upload_id": "upload-a",
                "provenance": omitted_test_provenance(),
                "sha256": "a" * 64,
            },
            {
                "path": "voice/b.mp3",
                "bytes": 1,
                "file_upload_id": "upload-b",
                "provenance": omitted_test_provenance(),
                "sha256": "a" * 64,
            },
            {
                "path": "voice/c.mp3",
                "bytes": 1,
                "file_upload_id": "upload-c",
                "provenance": omitted_test_provenance(),
                "sha256": "a" * 64,
            },
        ],
    }
    groups = {"voice": {"output_mode": "audio", "tasks": ["archive_audio"]}}

    ready = server.processing_service.ready_eager_files(
        job,
        upload,
        groups,
        {"voice"},
        tmp_path / "archive",
    )

    assert ready is not None
    group_name, files = ready
    assert group_name == "voice"
    assert [file_state["path"] for file_state in files] == [
        "voice/a.mp3",
        "voice/b.mp3",
    ]


def test_claim_running_eager_batch_files_marks_running_batch_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    job = {
        "job_id": "job-1",
        "eager_archive": {
            "files": {},
            "batches": {
                "batch-1": {
                    "batch_id": "batch-1",
                    "state": "running",
                    "group": "camera",
                    "paths": ["camera/a.mp4"],
                    "started_at": "2026-06-05T00:00:00Z",
                }
            },
            "next_batch_number": 2,
        },
    }
    upload = {
        "input_upload_id": "upload-1",
        "files": [
            {
                "path": "camera/a.mp4",
                "bytes": 1,
                "file_upload_id": "upload-a",
                "provenance": omitted_test_provenance(),
                "sha256": "a" * 64,
            }
        ],
    }

    changed = server.processing_service.claim_running_eager_batch_files(
        job,
        upload,
        {"camera": {}},
        tmp_path / "archive",
    )

    assert changed is True
    assert job["eager_archive"]["files"]["camera/a.mp4"]["state"] == "encoding"
    assert job["eager_archive"]["files"]["camera/a.mp4"]["batch_id"] == "batch-1"


def test_mark_existing_eager_outputs_does_not_complete_claimed_files(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    archive_dir = tmp_path / "archive"
    output = archive_dir / "camera" / "a.mkv"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"partial")
    job = {
        "job_id": "job-1",
        "eager_archive": {
            "files": {
                "camera/a.mp4": {
                    "state": "encoding",
                    "batch_id": "batch-1",
                }
            },
            "batches": {},
            "next_batch_number": 2,
        },
    }
    upload = server.upload_service.save_input_upload_raw(
        {
            "input_upload_id": "upload-1",
            "files": [
                {
                    "path": "camera/a.mp4",
                    "bytes": 1,
                    "file_upload_id": "upload-a",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                }
            ],
        }
    )

    updated_upload, changed = server.processing_service.mark_existing_eager_outputs(
        job,
        upload,
        {"camera": {}},
        {"camera"},
        archive_dir,
    )

    assert updated_upload["input_upload_id"] == upload["input_upload_id"]
    assert changed is False
    assert job["eager_archive"]["files"]["camera/a.mp4"]["state"] == "encoding"
    assert job["eager_archive"]["files"]["camera/a.mp4"]["batch_id"] == "batch-1"


def test_job_response_includes_eager_encode_progress(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    archive_dir = tmp_path / "archive"
    encoded_output = archive_dir / "camera" / "a.webm"
    active_output = archive_dir / "camera" / "b.webm"
    encoded_output.parent.mkdir(parents=True)
    encoded_output.write_bytes(b"encoded")
    active_output.write_bytes(b"active")
    server.upload_service.save_input_upload(
        {
            "input_upload_id": "upload-1",
            "files": [
                {
                    "path": "camera/a.mp4",
                    "bytes": 1024,
                    "file_upload_id": "upload-a",
                    "consumed_at": "2026-06-05T00:00:00Z",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                },
                {
                    "path": "camera/b.mp4",
                    "bytes": 2048,
                    "file_upload_id": "upload-b",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                },
            ],
        }
    )
    job = {
        "job_id": "job-1",
        "input_upload_id": "upload-1",
        "handoff": {"destination": "command", "options": {}},
        "groups": {
            "camera": {
                "output_mode": "video",
                "tasks": ["archive_video"],
                "eager_pipeline_batches": 1,
            }
        },
        "eager_archive": {
            "files": {
                "camera/a.mp4": {
                    "state": "encoded",
                    "group": "camera",
                    "input_bytes": 1024,
                    "output": str(encoded_output),
                    "output_bytes": 7,
                    "started_at": "2026-06-05T00:00:00Z",
                    "encoded_at": "2026-06-05T00:00:10Z",
                },
                "camera/b.mp4": {
                    "state": "encoding",
                    "group": "camera",
                    "input_bytes": 2048,
                    "output": str(active_output),
                    "started_at": "2026-06-05T00:00:05Z",
                },
            },
            "batches": {
                "batch-1": {
                    "state": "running",
                    "started_at": "2026-06-04T00:00:00Z",
                }
            },
        },
    }

    response = server.processing_service.job_response(job)
    upload_progress = response["upload_progress"]
    progress = response["encode_progress"]

    assert upload_progress["files_total"] == 2
    assert upload_progress["files_uploaded"] == 1
    assert upload_progress["bytes_total"] == 3072
    assert upload_progress["uploaded_bytes"] == 1024
    assert upload_progress["completed"] is False

    assert progress["files_total"] == 2
    assert progress["files_encoded"] == 1
    assert progress["files_encoding"] == 1
    assert progress["input_bytes_total"] == 3072
    assert progress["input_bytes_encoded"] == 1024
    assert progress["input_bytes_encoding"] == 2048
    assert progress["output_bytes"] == 7
    assert progress["active_output_bytes"] == 6
    assert progress["running_batches"] == 1
    assert progress["pipeline_batches"] == 1
    assert progress["started_at"] == "2026-06-04T00:00:00.000000Z"


def test_eager_encode_progress_ignores_sidecar_evidence_files(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    archive_dir = tmp_path / "archive"
    encoded_output = archive_dir / "camera" / "a.webm"
    encoded_output.parent.mkdir(parents=True)
    encoded_output.write_bytes(b"encoded")
    server.upload_service.save_input_upload(
        {
            "input_upload_id": "upload-1",
            "files": [
                {
                    "path": "camera/a.mp4",
                    "bytes": 1024,
                    "file_upload_id": "upload-a",
                    "consumed_at": "2026-06-05T00:00:00Z",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                },
                {
                    "path": "camera/a.xml",
                    "bytes": 12,
                    "file_upload_id": "upload-a-xml",
                    "route_action": "evidence",
                    "sidecar_for": "camera/a.mp4",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                },
            ],
        }
    )
    job = {
        "job_id": "job-1",
        "input_upload_id": "upload-1",
        "handoff": {"destination": "command", "options": {}},
        "groups": {
            "camera": {
                "output_mode": "video",
                "tasks": ["archive_video"],
            }
        },
        "eager_archive": {
            "files": {
                "camera/a.mp4": {
                    "state": "encoded",
                    "group": "camera",
                    "input_bytes": 1024,
                    "output": str(encoded_output),
                    "output_bytes": 7,
                    "started_at": "2026-06-05T00:00:00Z",
                    "encoded_at": "2026-06-05T00:00:10Z",
                },
            },
            "batches": {},
        },
    }

    progress = server.processing_service.job_response(job)["encode_progress"]

    assert progress["files_total"] == 1
    assert progress["files_encoded"] == 1
    assert progress["input_bytes_total"] == 1024
    assert progress["input_bytes_encoded"] == 1024
    assert progress["completed"] is True


def test_job_response_includes_review_clip_progress_from_gpu_status(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    job = {
        "job_id": "job-1",
        "state": "running",
        "phase": "review",
        "handoff": {"destination": "command", "options": {}},
        "gpu_statuses": {
            "gpu-job-1": {
                "state": "running",
                "items": {
                    "qcut_video": {
                        "status": "running",
                        "progress": {
                            "mode": "qcut_video",
                            "task": "qcut_video",
                            "phase": "encoding_clips",
                            "clips_total": 40,
                            "clips_done": 10,
                            "clips_running": 2,
                            "clips_failed": 0,
                            "percent_clips": 25.0,
                            "output_bytes": 1048576,
                            "active_output_bytes": 262144,
                            "output_rate_bytes_per_second": 131072,
                            "started_at": "2026-06-05T00:00:00Z",
                        },
                    }
                },
            }
        },
    }

    response = server.processing_service.job_response(job)
    progress = response["encode_progress"]

    assert progress["mode"] == "qcut_video"
    assert progress["phase"] == "encoding_clips"
    assert progress["clips_total"] == 40
    assert progress["clips_done"] == 10
    assert progress["clips_running"] == 2
    assert progress["files_total"] == 40
    assert progress["files_encoded"] == 10
    assert progress["files_encoding"] == 2
    assert progress["percent_clips"] == 25.0
    assert progress["percent_input_bytes"] == 25.0
    assert progress["output_bytes"] == 1048576
    assert progress["active_output_bytes"] == 262144
    assert progress["output_rate_bytes_per_second"] == 131072


def test_compact_job_response_keeps_operational_fields_only(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    server.upload_service.save_input_upload(
        {
            "input_upload_id": "upload-1",
            "files": [
                {
                    "path": "camera/a.mp4",
                    "bytes": 1,
                    "file_upload_id": "upload-a",
                    "provenance": omitted_test_provenance(),
                    "sha256": "a" * 64,
                }
            ],
        }
    )
    job = {
        "job_id": "job-1",
        "state": "running",
        "phase": "eager_archive:pipeline=1/3",
        "input_upload_id": "upload-1",
        "template_id": "camera-archive",
        "handoff": {"destination": "command", "options": {}},
        "eager_archive": {
            "files": {},
            "batches": {"batch-1": {"state": "running"}},
            "gpu_results": {"batch-0": {"large": ["payload"]}},
        },
        "gpu_result": {"large": ["payload"]},
    }

    compact = server.processing_service.compact_job_response(job)

    assert compact["job_id"] == "job-1"
    assert compact["state"] == "running"
    assert compact["phase"] == "eager_archive:pipeline=1/3"
    assert compact["upload_progress"]["files_total"] == 1
    assert "eager_archive" not in compact
    assert "gpu_result" not in compact


def test_list_jobs_returns_recent_compact_jobs(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    server.state_store.save_job(
        {
            "job_id": "done",
            "state": "succeeded",
            "phase": "done",
            "updated_at": "2026-01-01T00:00:00Z",
            "handoff": {"destination": "command", "options": {}},
            "eager_archive": {"gpu_results": {"large": ["payload"]}},
        }
    )
    server.state_store.save_job(
        {
            "job_id": "active",
            "state": "running",
            "phase": "eager_archive:pipeline=1/3",
            "updated_at": "2026-01-02T00:00:00Z",
            "handoff": {"destination": "command", "options": {}},
        }
    )

    active = server.list_jobs()
    all_jobs = server.list_jobs(terminal="all")
    all_jobs_single_page = server.list_jobs(
        terminal="all",
        page=2,
        per_page=1,
        all_items=True,
    )

    assert [job["job_id"] for job in active["jobs"]] == ["active"]
    assert active["page"] == 1
    assert active["per_page"] == 25
    assert active["total"] == 1
    assert active["pages"] == 1
    assert [job["job_id"] for job in all_jobs["jobs"]] == ["active", "done"]
    assert all_jobs["total"] == 2
    assert "eager_archive" not in all_jobs["jobs"][1]
    assert all_jobs_single_page["page"] == 1
    assert all_jobs_single_page["pages"] == 1
    assert all_jobs_single_page["per_page"] == 2
    assert [job["job_id"] for job in all_jobs_single_page["jobs"]] == ["active", "done"]


def test_list_jobs_pages_filters_and_searches_indexed_summaries(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    server.state_store.save_job(
        {
            "job_id": "job-1",
            "state": "running",
            "phase": "encoding",
            "template_id": "front-camera-archive",
            "input_upload_id": "upload-1",
            "workflow_mode": "collection_archive",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-03T00:00:00Z",
            "handoff": {"destination": "riverhog", "options": {}},
        }
    )
    server.state_store.save_job(
        {
            "job_id": "job-2",
            "state": "queued",
            "phase": "queued",
            "input_upload_id": "upload-2",
            "template_id": "back-camera-review",
            "workflow_mode": "review",
            "handoff": {"destination": "command", "options": {}},
            "review": {
                "route_id": "back-camera",
                "profile_id": "webm-q42",
            },
            "created_at": "2026-01-02T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
        }
    )
    server.state_store.save_job(
        {
            "job_id": "job-3",
            "state": "succeeded",
            "phase": "done",
            "template_id": "front-camera-archive",
            "input_upload_id": "upload-3",
            "workflow_mode": "collection_archive",
            "created_at": "2026-01-03T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "handoff": {"destination": "riverhog", "options": {}},
        }
    )

    page = server.list_jobs(
        terminal="all",
        q="front",
        workflow_mode="collection_archive",
        handoff_destination="riverhog",
        sort="created_at",
        order="asc",
        page=1,
        per_page=1,
    )

    assert page["total"] == 2
    assert page["pages"] == 2
    assert page["query"] == "front"
    assert page["filters"]["workflow_mode"] == "collection_archive"
    assert page["filters"]["handoff_destination"] == "riverhog"
    assert [job["job_id"] for job in page["jobs"]] == ["job-1"]

    second_page = server.list_jobs(
        terminal="all",
        q="front",
        workflow_mode="collection_archive",
        handoff_destination="riverhog",
        sort="created_at",
        order="asc",
        page=2,
        per_page=1,
    )

    assert [job["job_id"] for job in second_page["jobs"]] == ["job-3"]


def test_list_jobs_does_not_scan_all_job_states_for_current_page(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    server.state_store.save_job(
        {
            "job_id": "queued",
            "state": "queued",
            "phase": "queued",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "handoff": {"destination": "command", "options": {}},
        }
    )

    def fail_job_states() -> list[dict[str, object]]:
        raise AssertionError("job list must not materialize every job state")

    monkeypatch.setattr(server.scheduling_service, "job_states", fail_job_states)

    page = server.list_jobs(terminal="all")

    assert [job["job_id"] for job in page["jobs"]] == ["queued"]
    assert "queue" not in page["jobs"][0]


def test_acquire_job_gpu_reuses_persisted_lease_token(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    server.persistence.initialize_persistence()
    requests: list[dict[str, object]] = []

    def fake_manager_request(
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        assert method == "POST"
        assert path == "/acquire"
        assert payload is not None
        requests.append(dict(payload))
        return {"lease_token": payload["lease_token"], "queued": False}

    monkeypatch.setattr(server.media_service, "manager_request", fake_manager_request)
    job = {"job_id": "job-1", "gpu_lease_token": "saved-token"}
    server.state_store.save_job(job)

    token = server.media_service.acquire_job_gpu(job)

    assert token == "saved-token"
    assert requests[0]["lease_token"] == "saved-token"
    assert job["gpu_lease_token"] == "saved-token"


def test_munchy_tus_paths_resolve_under_the_configured_upload_root(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    server.upload_service.ensure_dirs()
    outside = tmp_path / "outside"
    outside.mkdir()
    (server.runtime_config.TUSD_DIR / "linked").symlink_to(outside, target_is_directory=True)

    assert server.upload_service.tusd_data_path("upload-a") == (
        server.runtime_config.TUSD_DIR / "upload-a"
    )
    with pytest.raises(RuntimeError, match="TUS upload escaped its configured root"):
        server.upload_service.tusd_data_path("linked/upload-a")


def test_metadata_projection_atomic_writes_are_owner_readable(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    destination = tmp_path / "archive" / "clip.xmp"

    assert server.routing_service.write_atomic_text(destination, "<xmpmeta />\n") is True
    assert destination.read_text(encoding="utf-8") == "<xmpmeta />\n"
    assert destination.stat().st_mode & 0o777 == 0o600


def test_retention_reports_stable_item_failures(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_server(tmp_path, monkeypatch)
    before = {
        "policy": {},
        "terminal_jobs": {"sample_job_ids": ["terminal-job"]},
        "job_diagnostics": {"sample_job_ids": ["diagnostic-job"]},
    }
    remaining = {
        "policy": {},
        "terminal_jobs": {"eligible": 1, "sample_job_ids": ["terminal-job"]},
        "job_diagnostics": {"eligible": 1, "sample_job_ids": ["diagnostic-job"]},
    }

    def retention_plan(*, candidate_limit: int = 10) -> dict[str, object]:
        if candidate_limit == server.runtime_config.RETENTION_BATCH_SIZE:
            return before
        return remaining

    monkeypatch.setattr(server.retention_service, "retention_plan", retention_plan)
    monkeypatch.setattr(
        server.diagnostic_service,
        "remove_job_diagnostic",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("private diagnostic path")),
    )
    monkeypatch.setattr(
        server.diagnostic_service,
        "remove_terminal_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("private database detail")),
    )

    result = server.retention_service.apply_retention()

    assert result == {
        "policy": {},
        "removed": {
            "terminal_jobs": 0,
            "terminal_job_ids": [],
            "job_diagnostics": 0,
            "job_diagnostic_ids": [],
        },
        "errors": [
            {
                "resource": "job_diagnostic",
                "job_id": "diagnostic-job",
                "code": "removal_failed",
                "message": "retention removal failed",
            },
            {
                "resource": "terminal_job",
                "job_id": "terminal-job",
                "code": "removal_failed",
                "message": "retention removal failed",
            },
        ],
        "remaining": remaining,
    }

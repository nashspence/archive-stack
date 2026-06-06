from __future__ import annotations

import importlib.util
import logging
import sys
import uuid
from pathlib import Path
from types import ModuleType

from munchy.uvicorn_logging import DropHealthAccessLogFilter


def load_runner(tmp_path: Path, monkeypatch) -> ModuleType:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MUNCHY_RUNNER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MUNCHY_RUNNER_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("MUNCHY_RUNNER_TUSD_DIR", str(tmp_path / "tusd"))
    monkeypatch.setenv("MUNCHY_RUNNER_GPU_RUNTIME_DIR", str(tmp_path / "gpu-runtime"))
    module_path = Path(__file__).resolve().parents[1] / "app" / "main.py"
    module_name = f"munchy_runner_main_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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


def test_capabilities_advertise_munchy_profile_target(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)

    capabilities = runner.capabilities()

    assert capabilities["encode_profile"]["targets"] == ["munchy-av1-nvenc"]
    assert capabilities["profile_groups"]["input_path_shape"] == "<profile-group>/<file>"
    assert capabilities["storage"]["eager_archive_only_encoding"] is True
    assert "MUNCHY_RUNNER_NOTIFY_WEBHOOKS" in capabilities["notify"]["webhook_config"]
    assert capabilities["storage"]["eager_archive_pipeline_batches"] == 3
    assert capabilities["notify"]["default_enabled"] is False
    assert capabilities["notify"]["default_recipients"] == []
    assert capabilities["notify"]["client_preflight_failed"] is True
    assert capabilities["operations"]["notify_preflight_failed"] is True


def test_notification_defaults_come_from_runner_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MUNCHY_RUNNER_NOTIFY_ENABLED", "1")
    monkeypatch.setenv("MUNCHY_RUNNER_NOTIFY_DEFAULT_RECIPIENTS", "operator")
    runner = load_runner(tmp_path, monkeypatch)

    req = runner.CreateJobRequest(collection_slug="collection")
    capabilities = runner.capabilities()

    assert req.notify.enabled is True
    assert req.notify.recipients == ["operator"]
    assert capabilities["notify"]["default_enabled"] is True
    assert capabilities["notify"]["default_recipients"] == ["operator"]


def test_notification_payload_identifies_munchy_with_canonical_emoji(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)

    payload = runner.notify_payload(
        {
            "job_id": "job-1",
            "collection_slug": "collection",
            "collection_timestamp": "20260605T120000Z",
            "phase": "queued",
            "state": "queued",
        },
        event="job.received",
        message="Job received.",
        severity="info",
        recipient="operator",
        extra=None,
    )

    assert payload["source"] == "munchy"
    assert payload["actor"] == "munchy"
    assert payload["event"] == "job.received"
    assert payload["delivered_at"].endswith("Z")
    assert payload["operator_urgency"] == "passive"
    assert payload["operator_action"] == "none"
    assert payload["notification"] == {
        "title": "🤤 collection",
        "body": "Munchy received this job and queued the work.",
    }


def test_client_preflight_failed_notification_uses_runner_defaults(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MUNCHY_RUNNER_NOTIFY_ENABLED", "1")
    monkeypatch.setenv("MUNCHY_RUNNER_NOTIFY_DEFAULT_RECIPIENTS", "operator")
    runner = load_runner(tmp_path, monkeypatch)
    calls: list[dict[str, object]] = []

    def fake_send_notify_deliveries(job, **kwargs):  # type: ignore[no-untyped-def]
        calls.append({"job": job, **kwargs})
        return [{"recipient": "operator", "status": 200}]

    monkeypatch.setattr(runner, "send_notify_deliveries", fake_send_notify_deliveries)

    result = runner.notify_preflight_failed(
        runner.ClientPreflightFailedNotificationRequest(
            message="Local media preflight failed for camera.",
            device_id="camera",
            workflow_mode="collection_preview",
            profile_group="camera-video",
            collection_slug="camera-preview",
            collection_timestamp="20260606T120000Z",
            upload_id="upload-1",
            job_id="job-1",
            files=2,
            failed_file_count=1,
            failed_files=[
                {
                    "path": "camera-video/bad.mp4",
                    "source": "/source/bad.mp4",
                    "issues": [{"code": "mp4_atom_extends_past_eof", "message": "bad atom"}],
                }
            ],
        )
    )

    assert result["status"] == "attempted"
    assert calls[0]["event"] == "job.issue"
    assert calls[0]["severity"] == "critical"
    assert calls[0]["recipients"] == ["operator"]
    assert calls[0]["job"]["phase"] == "preflight_failed"
    assert calls[0]["extra"]["component"] == "preflight"
    assert calls[0]["extra"]["error"] == "bad atom (bad.mp4)"
    assert calls[0]["extra"]["failed_file_count"] == 1


def test_preflight_issue_notification_error_keeps_truncated_filename_at_end(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    filename = "front-yard-camera-" + ("very-long-" * 12) + "clip.mp4"

    error = runner.preflight_issue_notification_error(
        path=f"reolink-video/2026/06/06/{filename}",
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
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    (runner.TUSD_DIR / "upload-b").write_bytes(b"b")
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
                }
            },
            "batches": {},
            "next_batch_number": 1,
        },
    }
    upload = {
        "upload_id": "upload-1",
        "files": [
            {"path": "camera/a.mp4", "bytes": 1, "upload_id": "upload-a"},
            {"path": "camera/b.mp4", "bytes": 1, "upload_id": "upload-b"},
            {"path": "camera/c.mp4", "bytes": 1, "upload_id": "upload-c"},
        ],
    }

    ready = runner.ready_eager_files(
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


def test_claim_running_eager_batch_files_marks_legacy_running_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
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
        "upload_id": "upload-1",
        "files": [{"path": "camera/a.mp4", "bytes": 1, "upload_id": "upload-a"}],
    }

    changed = runner.claim_running_eager_batch_files(
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
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
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
    upload = {
        "upload_id": "upload-1",
        "files": [{"path": "camera/a.mp4", "bytes": 1, "upload_id": "upload-a"}],
    }

    updated_upload, changed = runner.mark_existing_eager_outputs(
        job,
        upload,
        {"camera": {}},
        {"camera"},
        archive_dir,
    )

    assert updated_upload is upload
    assert changed is False
    assert job["eager_archive"]["files"]["camera/a.mp4"]["state"] == "encoding"
    assert job["eager_archive"]["files"]["camera/a.mp4"]["batch_id"] == "batch-1"


def test_job_response_includes_eager_encode_progress(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    archive_dir = tmp_path / "archive"
    encoded_output = archive_dir / "camera" / "a.webm"
    active_output = archive_dir / "camera" / "b.webm"
    encoded_output.parent.mkdir(parents=True)
    encoded_output.write_bytes(b"encoded")
    active_output.write_bytes(b"active")
    runner.save_input_upload(
        {
            "upload_id": "upload-1",
            "files": [
                {
                    "path": "camera/a.mp4",
                    "bytes": 1024,
                    "upload_id": "upload-a",
                    "consumed_at": "2026-06-05T00:00:00Z",
                },
                {
                    "path": "camera/b.mp4",
                    "bytes": 2048,
                    "upload_id": "upload-b",
                },
            ],
        }
    )
    job = {
        "job_id": "job-1",
        "input_upload_id": "upload-1",
        "groups": {
            "camera": {
                "archive_mode": "av1_nvenc",
                "gpu_tasks": ["archive_video"],
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

    progress = runner.job_response(job)["encode_progress"]

    assert progress["files_total"] == 2
    assert progress["files_encoded"] == 1
    assert progress["files_encoding"] == 1
    assert progress["input_bytes_total"] == 3072
    assert progress["input_bytes_encoded"] == 1024
    assert progress["input_bytes_encoding"] == 2048
    assert progress["output_bytes"] == 7
    assert progress["active_output_bytes"] == 6
    assert progress["running_batches"] == 1
    assert progress["pipeline_batches"] == 3
    assert progress["started_at"] == "2026-06-04T00:00:00Z"


def test_acquire_job_gpu_reuses_persisted_lease_token(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
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

    monkeypatch.setattr(runner, "manager_request", fake_manager_request)
    job = {"job_id": "job-1", "gpu_lease_token": "saved-token"}
    runner.save_job(job)

    token = runner.acquire_job_gpu(job)

    assert token == "saved-token"
    assert requests[0]["lease_token"] == "saved-token"
    assert job["gpu_lease_token"] == "saved-token"

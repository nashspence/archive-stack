from __future__ import annotations

import importlib.util
import json
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
    assert capabilities["storage"]["max_running_jobs"] == 1
    assert capabilities["notify"]["default_enabled"] is False
    assert capabilities["notify"]["default_recipients"] == []
    assert capabilities["notify"]["client_preflight_failed"] is True
    assert capabilities["operations"]["notify_preflight_failed"] is True


def test_scheduler_reserves_running_job_slots_and_leaves_extra_jobs_queued(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MUNCHY_RUNNER_MAX_RUNNING_JOBS", "1")
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    runner.save_job(
        {
            "job_id": "job-a",
            "state": "queued",
            "phase": "queued",
            "created_at": "2026-01-01T00:00:00Z",
        }
    )
    runner.save_job(
        {
            "job_id": "job-b",
            "state": "queued",
            "phase": "queued",
            "created_at": "2026-01-01T00:00:01Z",
        }
    )
    scheduled_tasks: list[tuple[object, tuple[object, ...]]] = []

    class Tasks:
        def add_task(self, func, *args):  # type: ignore[no-untyped-def]
            scheduled_tasks.append((func, args))

    first = runner.schedule_pending_jobs(Tasks())
    second = runner.schedule_pending_jobs(Tasks())
    job_b = runner.job_response(runner.load_job("job-b"))

    assert first == ["job-a"]
    assert second == []
    assert scheduled_tasks == [(runner.run_job, ("job-a",))]
    assert runner.scheduler_status()["scheduled_jobs"] == ["job-a"]
    assert job_b["queue"]["position"] == 2
    assert job_b["queue"]["running_job_limit"] == 1


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


def test_upload_waiting_reminder_is_time_sensitive_and_paced(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MUNCHY_RUNNER_NOTIFY_ENABLED", "1")
    monkeypatch.setenv("MUNCHY_RUNNER_NOTIFY_DEFAULT_RECIPIENTS", "operator")
    monkeypatch.setenv("MUNCHY_RUNNER_NOTIFY_UPLOAD_WAITING_REMINDER_SECONDS", "1")
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    calls: list[dict[str, object]] = []

    def fake_send_notify_deliveries(job, **kwargs):  # type: ignore[no-untyped-def]
        calls.append({"job": job, **kwargs})
        return [{"recipient": "operator", "status": 200}]

    monkeypatch.setattr(runner, "send_notify_deliveries", fake_send_notify_deliveries)
    job = {
        "job_id": "job-1",
        "collection_slug": "backyard-preview",
        "collection_timestamp": "20260606T120000Z",
        "state": "running",
        "phase": "waiting_for_eager_files:1/2",
        "notify": {
            "enabled": True,
            "recipients": ["operator"],
            "events": runner.DEFAULT_NOTIFY_EVENTS,
        },
    }
    runner.save_job(job)
    upload = {
        "upload_id": "upload-1",
        "created_at": "2026-01-01T00:00:00Z",
        "files": [
            {"path": "camera/a.mp4", "bytes": 1, "upload_id": "upload-a"},
            {"path": "camera/b.mp4", "bytes": 1, "upload_id": "upload-b"},
        ],
    }
    progress = {
        "files_total": 2,
        "files_uploaded": 1,
        "bytes_total": 2,
        "uploaded_bytes": 1,
    }

    first = runner.notify_upload_waiting_reminder(job, upload, progress)
    second = runner.notify_upload_waiting_reminder(job, upload, progress)

    assert first["status"] == "attempted"
    assert second["status"] == "suppressed"
    assert second["reason"] == "reminder_repeat_limit"
    assert calls[0]["event"] == "job.upload_waiting.reminder"
    assert calls[0]["severity"] == "warning"
    assert calls[0]["extra"]["reminder_count"] == 1
    assert calls[0]["extra"]["reminder_interval_seconds"] == 1
    assert calls[0]["extra"]["upload_progress"]["files_uploaded"] == 1


def test_load_input_upload_does_not_refresh_state_timestamp(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    runner.save_input_upload(
        {
            "upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "files": [{"path": "camera/a.mp4", "bytes": 1, "upload_id": "upload-a"}],
        }
    )
    before = runner.read_state("input-upload", "upload-1")["updated_at"]

    loaded = runner.load_input_upload("upload-1")

    after = runner.read_state("input-upload", "upload-1")["updated_at"]
    assert loaded["files_total"] == 1
    assert after == before


def test_cancel_job_with_cleanup_removes_local_work_and_input_upload(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    data_path = runner.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"a")
    shared_root = runner.shared_input_upload_root("upload-1")
    shared_root.mkdir(parents=True)
    (shared_root / "camera").mkdir()
    (shared_root / "camera" / "a.mp4").write_bytes(b"a")
    work_dir = runner.GPU_RUNTIME_DIR / "jobs" / "job-1"
    work_dir.mkdir(parents=True)
    (work_dir / "scratch.txt").write_text("scratch", encoding="utf-8")
    runner.save_input_upload(
        {
            "upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "files": [{"path": "camera/a.mp4", "bytes": 1, "upload_id": "upload-a"}],
        }
    )
    runner.save_job(
        {
            "job_id": "job-1",
            "state": "queued",
            "phase": "queued",
            "input_upload_id": "upload-1",
            "groups": {"camera": {}},
        }
    )

    job = runner.cancel_job("job-1", cleanup=True)

    assert job["state"] == "cancelled"
    assert job["cleanup_requested"] is True
    assert runner.read_state("input-upload", "upload-1") is None
    assert not data_path.exists()
    assert not shared_root.exists()
    assert not work_dir.exists()
    assert "input-upload:upload-1" in job["cleanup_removed"]


def test_cancel_job_with_cleanup_preserves_input_upload_referenced_by_sibling(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    data_path = runner.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"a")
    shared_root = runner.shared_input_upload_root("upload-1")
    shared_root.mkdir(parents=True)
    runner.save_input_upload(
        {
            "upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "files": [{"path": "camera/a.mp4", "bytes": 1, "upload_id": "upload-a"}],
        }
    )
    runner.save_job(
        {
            "job_id": "job-1",
            "state": "queued",
            "phase": "queued",
            "input_upload_id": "upload-1",
            "groups": {"camera": {}},
        }
    )
    runner.save_job(
        {
            "job_id": "job-2",
            "state": "queued",
            "phase": "queued",
            "input_upload_id": "upload-1",
            "groups": {"camera": {}},
        }
    )

    job = runner.cancel_job("job-1", cleanup=True)

    assert job["state"] == "cancelled"
    assert runner.read_state("input-upload", "upload-1") is not None
    assert data_path.exists()
    assert shared_root.exists()
    assert "input-upload:upload-1" not in job.get("cleanup_removed", [])


def test_failed_job_with_cleanup_removes_local_work_and_input_upload(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    data_path = runner.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"a")
    shared_root = runner.shared_input_upload_root("upload-1")
    shared_root.mkdir(parents=True)
    (shared_root / "camera").mkdir()
    (shared_root / "camera" / "a.mp4").write_bytes(b"a")
    work_dir = runner.GPU_RUNTIME_DIR / "jobs" / "job-1"
    work_dir.mkdir(parents=True)
    (work_dir / "scratch.txt").write_text("scratch", encoding="utf-8")
    runner.save_input_upload(
        {
            "upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "files": [{"path": "camera/a.mp4", "bytes": 1, "upload_id": "upload-a"}],
        }
    )
    runner.save_job(
        {
            "job_id": "job-1",
            "state": "failed",
            "phase": "gpu",
            "finished_at": "2026-01-01T00:00:00Z",
            "input_upload_id": "upload-1",
            "groups": {"camera": {}},
        }
    )

    job = runner.cancel_job("job-1", cleanup=True)

    assert job["state"] == "failed"
    assert runner.read_state("input-upload", "upload-1") is None
    assert not data_path.exists()
    assert not shared_root.exists()
    assert not work_dir.exists()
    assert "input-upload:upload-1" in job["cleanup_removed"]


def test_cleanup_once_removes_old_failed_job_work_and_input_upload(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    data_path = runner.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"a")
    shared_root = runner.shared_input_upload_root("upload-1")
    shared_root.mkdir(parents=True)
    (shared_root / "camera").mkdir()
    (shared_root / "camera" / "a.mp4").write_bytes(b"a")
    work_dir = runner.GPU_RUNTIME_DIR / "jobs" / "job-1"
    work_dir.mkdir(parents=True)
    (work_dir / "scratch.txt").write_text("scratch", encoding="utf-8")
    runner.save_input_upload(
        {
            "upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "files": [{"path": "camera/a.mp4", "bytes": 1, "upload_id": "upload-a"}],
        }
    )
    runner.save_job(
        {
            "job_id": "job-1",
            "state": "failed",
            "phase": "gpu",
            "finished_at": "2026-01-01T00:00:00Z",
            "input_upload_id": "upload-1",
            "groups": {"camera": {}},
        }
    )

    result = runner.cleanup_once()

    assert "job-cleanup:job-1" in result["removed"]
    assert runner.read_state("input-upload", "upload-1") is None
    assert not data_path.exists()
    assert not shared_root.exists()
    assert not work_dir.exists()


def test_prepare_shared_input_tree_links_uploaded_files_without_sha_rehash(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    data_path = runner.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"video")
    monkeypatch.setattr(
        runner,
        "file_sha256",
        lambda path: (_ for _ in ()).throw(AssertionError("sha256 should not run")),
    )
    upload = {
        "upload_id": "upload-1",
        "files": [
            {
                "path": "camera/a.mp4",
                "bytes": 5,
                "sha256": "0" * 64,
                "upload_id": "upload-a",
            }
        ],
    }

    root = runner.prepare_shared_input_tree(upload, {"camera"})

    linked = root / "camera" / "a.mp4"
    assert linked.read_bytes() == b"video"
    assert linked.stat().st_ino == data_path.stat().st_ino
    marker = root / ".munchy-input-upload.json"
    metadata = json.loads(marker.read_text(encoding="utf-8"))
    assert metadata["upload_id"] == "upload-1"
    assert metadata["files"] == 1
    monkeypatch.setattr(
        runner,
        "materialize_upload_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("prepared shared input tree should be reused")
        ),
    )

    assert runner.prepare_shared_input_tree(upload, {"camera"}) == root


def test_sync_shared_input_tree_links_completed_files_incrementally(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    complete_path = runner.tusd_data_path("upload-a")
    partial_path = runner.tusd_data_path("upload-b")
    complete_path.parent.mkdir(parents=True, exist_ok=True)
    complete_path.write_bytes(b"video-a")
    partial_path.write_bytes(b"vid")
    upload = {
        "upload_id": "upload-1",
        "files": [
            {"path": "camera/a.mp4", "bytes": 7, "upload_id": "upload-a"},
            {"path": "camera/b.mp4", "bytes": 7, "upload_id": "upload-b"},
        ],
    }

    summary = runner.sync_shared_input_tree(upload, {"camera"})
    progress = runner.upload_group_progress(upload, {"camera"})
    root = runner.shared_input_upload_root("upload-1")

    assert summary["linked"] == 1
    assert (root / "camera" / "a.mp4").read_bytes() == b"video-a"
    assert not (root / "camera" / "b.mp4").exists()
    assert progress["files_uploaded"] == 1
    assert progress["input_tree_files_ready"] == 1

    partial_path.write_bytes(b"video-b")
    summary = runner.sync_shared_input_tree(upload, {"camera"})
    progress = runner.upload_group_progress(upload, {"camera"})

    assert summary["linked"] == 2
    assert (root / "camera" / "b.mp4").read_bytes() == b"video-b"
    assert progress["files_uploaded"] == 2
    assert progress["input_tree_files_ready"] == 2


def test_run_job_points_gpu_payload_at_shared_input_tree(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    data_path = runner.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"video")
    runner.save_input_upload(
        {
            "upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "storage_hint": {
                "workflow_mode": "review_only",
                "archive_mode": "av1_nvenc",
                "gpu_tasks": ["qcut_video"],
                "groups": {"camera": {"archive_mode": "av1_nvenc", "gpu_tasks": ["qcut_video"]}},
            },
            "files": [{"path": "camera/a.mp4", "bytes": 5, "upload_id": "upload-a"}],
        }
    )
    runner.save_job(
        {
            "job_id": "job-1",
            "state": "queued",
            "phase": "queued",
            "workflow_mode": "review_only",
            "input_upload_id": "upload-1",
            "collection_slug": "camera-review",
            "groups": {
                "camera": {
                    "archive_mode": "av1_nvenc",
                    "gpu_tasks": ["qcut_video"],
                    "profile": "profile",
                }
            },
        }
    )
    payloads: list[dict[str, object]] = []
    monkeypatch.setattr(runner, "acquire_job_gpu", lambda job: "token")
    monkeypatch.setattr(runner, "release_job_gpu", lambda job, token: None)
    monkeypatch.setattr(runner, "start_gpu_job", lambda payload: payloads.append(payload))
    monkeypatch.setattr(
        runner,
        "wait_gpu_job",
        lambda gpu_job_id, *, gpu_payload, job: {"state": "succeeded"},
    )
    monkeypatch.setattr(runner, "upload_review", lambda *args, **kwargs: {"returncode": 0})
    monkeypatch.setattr(runner, "notify_job_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "notify_job_issue", lambda *args, **kwargs: None)

    runner.run_job("job-1")

    assert len(payloads) == 1
    assert str(payloads[0]["input_dir"]).startswith("/data/input-uploads/")
    assert str(payloads[0]["input_dir"]).endswith("/camera")
    assert "/jobs/job-1/input" not in str(payloads[0]["input_dir"])
    assert runner.load_job("job-1")["state"] == "succeeded"


def test_run_job_reuses_stored_shared_review_plan(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    data_path = runner.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"video")
    plan = {
        "kind": "munchy.qcut-plan",
        "version": 1,
        "clips": [{"index": 1, "source": "/data/input-uploads/upload/camera/a.mp4"}],
        "files": [{"path": "/data/input-uploads/upload/camera/a.mp4"}],
    }
    runner.store_shared_review_plan("upload-1", "camera", "qcut_video", plan)
    runner.save_input_upload(
        {
            "upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "storage_hint": {
                "workflow_mode": "review_only",
                "archive_mode": "av1_nvenc",
                "gpu_tasks": ["qcut_video"],
                "groups": {"camera": {"archive_mode": "av1_nvenc", "gpu_tasks": ["qcut_video"]}},
            },
            "files": [{"path": "camera/a.mp4", "bytes": 5, "upload_id": "upload-a"}],
        }
    )
    runner.save_job(
        {
            "job_id": "job-2",
            "state": "queued",
            "phase": "queued",
            "workflow_mode": "review_only",
            "input_upload_id": "upload-1",
            "collection_slug": "camera-review-q43",
            "groups": {
                "camera": {
                    "archive_mode": "av1_nvenc",
                    "gpu_tasks": ["qcut_video"],
                    "profile": "profile",
                }
            },
        }
    )
    payloads: list[dict[str, object]] = []
    monkeypatch.setattr(runner, "acquire_job_gpu", lambda job: "token")
    monkeypatch.setattr(runner, "release_job_gpu", lambda job, token: None)
    monkeypatch.setattr(runner, "start_gpu_job", lambda payload: payloads.append(payload))
    monkeypatch.setattr(
        runner,
        "wait_gpu_job",
        lambda gpu_job_id, *, gpu_payload, job: {"state": "succeeded"},
    )
    monkeypatch.setattr(runner, "upload_review", lambda *args, **kwargs: {"returncode": 0})
    monkeypatch.setattr(runner, "notify_job_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "notify_job_issue", lambda *args, **kwargs: None)

    runner.run_job("job-2")

    assert payloads[0]["review_plans"]["qcut_video"]["kind"] == "munchy.qcut-plan"  # type: ignore[index]
    assert payloads[0]["review_plans"]["qcut_video"]["shared_plan"]["upload_id"] == "upload-1"  # type: ignore[index]


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

    response = runner.job_response(job)
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
    assert progress["pipeline_batches"] == 3
    assert progress["started_at"] == "2026-06-04T00:00:00Z"


def test_job_response_includes_review_clip_progress_from_gpu_status(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    job = {
        "job_id": "job-1",
        "state": "running",
        "phase": "review",
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

    response = runner.job_response(job)
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
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    runner.save_input_upload(
        {
            "upload_id": "upload-1",
            "files": [{"path": "camera/a.mp4", "bytes": 1, "upload_id": "upload-a"}],
        }
    )
    job = {
        "job_id": "job-1",
        "state": "running",
        "phase": "gpu-eager:pipeline=1/3",
        "input_upload_id": "upload-1",
        "collection_slug": "camera-preview",
        "eager_archive": {
            "files": {},
            "batches": {"batch-1": {"state": "running"}},
            "gpu_results": {"batch-0": {"large": ["payload"]}},
        },
        "gpu_result": {"large": ["payload"]},
    }

    compact = runner.compact_job_response(job)

    assert compact["job_id"] == "job-1"
    assert compact["state"] == "running"
    assert compact["phase"] == "gpu-eager:pipeline=1/3"
    assert compact["upload_progress"]["files_total"] == 1
    assert "eager_archive" not in compact
    assert "gpu_result" not in compact


def test_list_jobs_returns_recent_compact_jobs(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    runner.save_job(
        {
            "job_id": "done",
            "state": "succeeded",
            "phase": "done",
            "updated_at": "2026-01-01T00:00:00Z",
            "eager_archive": {"gpu_results": {"large": ["payload"]}},
        }
    )
    runner.save_job(
        {
            "job_id": "active",
            "state": "running",
            "phase": "gpu-eager:pipeline=1/3",
            "updated_at": "2026-01-02T00:00:00Z",
        }
    )

    active = runner.list_jobs()
    all_jobs = runner.list_jobs(include_terminal=True)

    assert [job["job_id"] for job in active["jobs"]] == ["active"]
    assert active["count"] == 1
    assert active["total_matching"] == 1
    assert [job["job_id"] for job in all_jobs["jobs"]] == ["active", "done"]
    assert "eager_archive" not in all_jobs["jobs"][1]


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

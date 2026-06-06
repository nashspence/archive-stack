from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from types import ModuleType


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


def test_capabilities_advertise_munchy_profile_target(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)

    capabilities = runner.capabilities()

    assert capabilities["encode_profile"]["targets"] == ["munchy-av1-nvenc"]
    assert capabilities["profile_groups"]["input_path_shape"] == "<profile-group>/<file>"
    assert capabilities["storage"]["eager_archive_only_encoding"] is True
    assert "MUNCHY_RUNNER_NOTIFY_WEBHOOKS" in capabilities["notify"]["webhook_config"]
    assert capabilities["storage"]["eager_archive_pipeline_batches"] == 3


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
    assert payload["emoji"] == "🤤"
    assert payload["event"] == "job.received"


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

#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

RUNNER_URL = os.getenv("MUNCHY_RUNNER_SMOKE_URL", "http://127.0.0.1:8092").rstrip("/")
GPU_TARGET_URL = os.getenv("MUNCHY_RUNNER_SMOKE_GPU_TARGET_URL", "http://127.0.0.1:8000").rstrip(
    "/"
)
RUNNER_CONTAINER = os.getenv("MUNCHY_RUNNER_SMOKE_CONTAINER", "munchy-runner")
GPU_TARGET_CONTAINER = os.getenv(
    "MUNCHY_RUNNER_SMOKE_GPU_TARGET_CONTAINER",
    "munchy-av1-nvenc-api-1",
)
STATE_DB = Path(os.getenv("MUNCHY_RUNNER_SMOKE_STATE_DB", "runtime/state/runner.sqlite3")).resolve()
GPU_RUNTIME_DIR = Path(
    os.getenv(
        "MUNCHY_RUNNER_SMOKE_GPU_RUNTIME_DIR",
        "../gpu-service-manager/runtime/munchy-av1-nvenc",
    )
).resolve()
SOURCE_PATH = Path(os.getenv("MUNCHY_RUNNER_GPU_SMOKE_SOURCE", "")).expanduser()
INTERRUPT_DELAY_SECONDS = float(os.getenv("MUNCHY_RUNNER_GPU_SMOKE_INTERRUPT_DELAY_SECONDS", "5"))
JOB_TIMEOUT_SECONDS = float(os.getenv("MUNCHY_RUNNER_GPU_SMOKE_JOB_TIMEOUT_SECONDS", "1800"))
KEEP_ARTIFACTS = os.getenv("MUNCHY_RUNNER_GPU_SMOKE_KEEP_ARTIFACTS", "0").lower() in {
    "1",
    "true",
    "yes",
    "on",
}


class HttpResult:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body

    def json(self) -> dict:
        return json.loads(self.body.decode("utf-8"))


def request(
    method: str, url: str, *, payload: dict | None = None, timeout: float = 60.0
) -> HttpResult:
    body = None
    headers = {}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return HttpResult(resp.status, resp.read())
    except urllib.error.HTTPError as exc:
        return HttpResult(exc.code, exc.read())


def api(method: str, path: str, *, payload: dict | None = None, expect: int = 200) -> dict:
    result = request(method, f"{RUNNER_URL}{path}", payload=payload)
    if result.status != expect:
        body = result.body.decode("utf-8", "replace")
        raise AssertionError(
            f"{method} {path}: expected HTTP {expect}, got {result.status}: {body}"
        )
    return result.json()


def target_api(method: str, path: str, *, expect: int = 200) -> dict | None:
    result = request(method, f"{GPU_TARGET_URL}{path}", timeout=10)
    if result.status == 404:
        return None
    if result.status != expect:
        body = result.body.decode("utf-8", "replace")
        raise AssertionError(
            f"GPU target {method} {path}: expected HTTP {expect}, got {result.status}: {body}"
        )
    return result.json()


def wait_ready(timeout_s: float = 120.0) -> dict:
    deadline = time.monotonic() + timeout_s
    last_error = ""
    while time.monotonic() < deadline:
        try:
            return api("GET", "/health/ready")
        except Exception as exc:
            last_error = str(exc)
            time.sleep(1)
    raise TimeoutError(f"runner did not become ready: {last_error}")


def wait_target_ready(timeout_s: float = 180.0) -> dict:
    deadline = time.monotonic() + timeout_s
    last_error = ""
    while time.monotonic() < deadline:
        try:
            ready = target_api("GET", "/health/ready")
            if ready is not None:
                return ready
        except Exception as exc:
            last_error = str(exc)
        time.sleep(1)
    raise TimeoutError(f"GPU target did not become ready: {last_error}")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def gpu_group_job_id(job_id: str, group_name: str) -> str:
    digest = hashlib.sha256(f"{job_id}/{group_name}".encode()).hexdigest()[:10]
    safe_group = group_name[:48]
    suffix = f"__{safe_group}__{digest}"
    return f"{job_id[: max(1, 180 - len(suffix))]}{suffix}"


def storage_hint_from_job_payload(payload: dict) -> dict:
    groups = {
        name: {
            "archive_mode": group.get("archive_mode", payload["archive_mode"]),
            "tasks": list(group.get("tasks") or []),
        }
        for name, group in dict(payload["groups"]).items()
    }
    return {
        "workflow_mode": payload.get("workflow_mode", "archive"),
        "archive_mode": payload["archive_mode"],
        "tasks": payload["tasks"],
        "groups": groups,
    }


def create_upload(upload_id: str, rel_path: str, source: Path, storage_hint: dict) -> None:
    api(
        "POST",
        "/v1/input-uploads",
        expect=201,
        payload={
            "upload_id": upload_id,
            "files": [
                {
                    "path": rel_path,
                    "bytes": source.stat().st_size,
                    "sha256": file_sha256(source),
                }
            ],
            "storage_hint": storage_hint,
        },
    )


def complete_upload(upload_id: str, rel_path: str, source: Path) -> None:
    upload = api("POST", f"/v1/input-uploads/{upload_id}/files/{rel_path}/upload", expect=201)
    upload_url = str(upload["upload_url"])
    offset = int(upload["offset"])
    length = int(upload["length"])
    if offset > length:
        raise AssertionError(f"TUS offset is beyond upload length: offset={offset} length={length}")
    if offset < length:
        cmd = [
            "curl",
            "-fsS",
            "-X",
            "PATCH",
            upload_url,
            "-H",
            "Tus-Resumable: 1.0.0",
            "-H",
            f"Upload-Offset: {offset}",
            "-H",
            "Content-Type: application/offset+octet-stream",
        ]
        if offset == 0:
            cmd.extend(["--data-binary", f"@{source}"])
        else:
            shell_cmd = (
                f"tail -c +{offset + 1} {shlex_quote(str(source))} | "
                + " ".join(shlex_quote(part) for part in cmd)
                + " --data-binary @-"
            )
            cmd = ["/bin/sh", "-c", shell_cmd]
        subprocess.run(cmd, check=True, timeout=900)
    status = api("GET", f"/v1/input-uploads/{upload_id}")
    if status["state"] != "uploaded":
        raise AssertionError(f"input upload did not complete: {status}")


def shlex_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def wait_job(job_id: str, predicate, *, timeout_s: float, label: str) -> dict:
    deadline = time.monotonic() + timeout_s
    last: dict | None = None
    while time.monotonic() < deadline:
        last = api("GET", f"/v1/jobs/{job_id}")
        if predicate(last):
            return last
        time.sleep(2)
    raise TimeoutError(f"timed out waiting for {label}: {last}")


def wait_target_job_running(job_id: str, *, timeout_s: float = 180.0) -> dict:
    deadline = time.monotonic() + timeout_s
    last: dict | None = None
    while time.monotonic() < deadline:
        try:
            status = target_api("GET", f"/v1/jobs/{job_id}")
        except Exception:
            status = None
        if status is not None:
            last = status
            if status.get("state") == "running":
                return status
            if status.get("state") == "succeeded":
                raise AssertionError(f"GPU job finished before restart could be tested: {status}")
            if status.get("state") == "failed":
                raise AssertionError(f"GPU job failed before restart could be tested: {status}")
        time.sleep(1)
    raise TimeoutError(f"timed out waiting for GPU target job to run: {last}")


def interrupt_gpu_target() -> None:
    # docker restart is intentionally graceful; Uvicorn waits for background
    # ffmpeg tasks. Kill/start simulates the crash-style restart this smoke
    # needs to prove the runner can recover from.
    subprocess.run(
        ["docker", "kill", GPU_TARGET_CONTAINER],
        text=True,
        capture_output=True,
        check=True,
        timeout=60,
    )
    subprocess.run(
        ["docker", "start", GPU_TARGET_CONTAINER],
        text=True,
        capture_output=True,
        check=True,
        timeout=120,
    )


def cleanup_via_runner_container(job_id: str, upload_id: str) -> None:
    code = r"""
import json
import shutil
import sqlite3
import sys
from pathlib import Path

job_id, upload_id = sys.argv[1], sys.argv[2]
state_db = Path("/state/runner.sqlite3")
tusd_dir = Path("/tusd")
gpu_runtime_dir = Path("/gpu-runtime/munchy-av1-nvenc")

with sqlite3.connect(state_db, timeout=30) as conn:
    row = conn.execute(
        "SELECT payload FROM states WHERE kind = ? AND id = ?",
        ("input-upload", upload_id),
    ).fetchone()
    if row is not None:
        payload = json.loads(row[0])
        for file_state in payload.get("files", []):
            tus_path = tusd_dir / str(file_state["upload_id"])
            tus_path.unlink(missing_ok=True)
            tus_path.with_suffix(tus_path.suffix + ".info").unlink(missing_ok=True)
            tus_path.with_suffix(tus_path.suffix + ".lock").unlink(missing_ok=True)
    conn.execute("DELETE FROM states WHERE kind = ? AND id = ?", ("job", job_id))
    conn.execute("DELETE FROM states WHERE kind = ? AND id = ?", ("input-upload", upload_id))

if job_id.startswith("smoke-gpu-restart-"):
    shutil.rmtree(gpu_runtime_dir / "jobs" / job_id, ignore_errors=True)
"""
    subprocess.run(
        ["docker", "exec", RUNNER_CONTAINER, "python", "-c", code, job_id, upload_id],
        text=True,
        capture_output=True,
        check=True,
        timeout=180,
    )


def main() -> int:
    if not SOURCE_PATH.is_file():
        raise SystemExit("set MUNCHY_RUNNER_GPU_SMOKE_SOURCE to a real video file")
    ready = wait_ready()
    if ready.get("riverhog_upload_enabled") or ready.get("review_upload_enabled"):
        raise SystemExit("refusing to run while Riverhog or review uploads are enabled")
    if ready.get("scheduler_paused"):
        raise SystemExit("refusing to run while runner scheduler is paused")
    wait_target_ready()

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    prefix = f"smoke-gpu-restart-{stamp}-{os.getpid()}"
    upload_id = f"{prefix}-input"
    job_id = f"{prefix}-job"
    group_name = "gpu-restart-smoke"
    rel_path = f"{group_name}/{SOURCE_PATH.name}"
    gpu_job_id = gpu_group_job_id(job_id, group_name)
    archive_output = (
        GPU_RUNTIME_DIR
        / "jobs"
        / job_id
        / "archive"
        / group_name
        / SOURCE_PATH.with_suffix(".mkv").name
    )

    job_payload = {
        "job_id": job_id,
        "input_upload_id": upload_id,
        "collection_slug": "gpu-restart-smoke",
        "collection_timestamp": stamp,
        "archive_mode": "av1_nvenc",
        "tasks": ["archive_video"],
        "encode_profile": {
            "schema_version": 1,
            "name": "gpu-restart-camera-720p",
            "archive": {
                "quality": 48,
                "max_height": 720,
                "fps_mode": "halve_60_to_30",
                "output_fps": 30,
                "scale_flags": "lanczos",
                "pix_fmt": "p010le",
                "audio": {
                    "bitrate": "28k",
                    "sample_rate": 24000,
                    "channels": 1,
                    "application": "audio",
                    "frame_duration": 40,
                    "cutoff": 12000,
                    "compression_level": 10,
                    "vbr": "on",
                },
            },
        },
        "groups": {
            group_name: {
                "archive_mode": "av1_nvenc",
                "tasks": ["archive_video"],
                "encode_profile": {
                    "schema_version": 1,
                    "name": "gpu-restart-camera-720p",
                    "archive": {
                        "quality": 48,
                        "max_height": 720,
                        "fps_mode": "halve_60_to_30",
                        "output_fps": 30,
                        "scale_flags": "lanczos",
                        "pix_fmt": "p010le",
                        "audio": {
                            "bitrate": "28k",
                            "sample_rate": 24000,
                            "channels": 1,
                            "application": "audio",
                            "frame_duration": 40,
                            "cutoff": 12000,
                            "compression_level": 10,
                            "vbr": "on",
                        },
                    },
                },
            },
        },
        "riverhog": {"enabled": False},
        "review_upload": {"enabled": False},
        "notify": {"enabled": False},
    }

    try:
        create_upload(upload_id, rel_path, SOURCE_PATH, storage_hint_from_job_payload(job_payload))
        complete_upload(upload_id, rel_path, SOURCE_PATH)
        api(
            "POST",
            "/v1/jobs",
            expect=202,
            payload=job_payload,
        )
        wait_job(
            job_id,
            lambda job: job.get("phase") == f"gpu:{group_name}",
            timeout_s=180,
            label="runner GPU phase",
        )
        target_before = wait_target_job_running(gpu_job_id)
        time.sleep(INTERRUPT_DELAY_SECONDS)
        interrupt_gpu_target()
        wait_target_ready()
        final = wait_job(
            job_id,
            lambda job: job.get("state") in {"succeeded", "failed", "cancelled"},
            timeout_s=JOB_TIMEOUT_SECONDS,
            label="runner terminal state after GPU target restart",
        )
        if final.get("state") != "succeeded":
            raise AssertionError(f"GPU restart job did not succeed: {final}")
        if not archive_output.exists() or archive_output.stat().st_size <= 0:
            raise AssertionError(f"archive output is missing or empty: {archive_output}")
        gpu_result = final.get("gpu_result") if isinstance(final.get("gpu_result"), dict) else {}
        print(
            json.dumps(
                {
                    "status": "ok",
                    "runner_url": RUNNER_URL,
                    "gpu_target_url": GPU_TARGET_URL,
                    "job": job_id,
                    "source": str(SOURCE_PATH),
                    "source_bytes": SOURCE_PATH.stat().st_size,
                    "target_state_before_restart": target_before.get("state"),
                    "archive_output": str(archive_output),
                    "archive_bytes": archive_output.stat().st_size,
                    "gpu_started_at": gpu_result.get("started_at"),
                    "gpu_finished_at": gpu_result.get("finished_at"),
                },
                indent=2,
            )
        )
    finally:
        if not KEEP_ARTIFACTS:
            try:
                api("POST", f"/v1/jobs/{job_id}/cancel", expect=202)
            except Exception:
                pass
            cleanup_via_runner_container(job_id, upload_id)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise

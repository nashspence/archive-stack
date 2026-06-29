#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

RUNNER_URL = os.getenv("MUNCHY_RUNNER_SMOKE_URL", "http://127.0.0.1:8092").rstrip("/")
RUNNER_CONTAINER = os.getenv("MUNCHY_RUNNER_SMOKE_CONTAINER", "munchy-runner")
STATE_DIR = Path(os.getenv("MUNCHY_RUNNER_SMOKE_STATE_DIR", "runtime/state")).resolve()
STATE_DB = Path(
    os.getenv("MUNCHY_RUNNER_SMOKE_STATE_DB", str(STATE_DIR / "runner.sqlite3"))
).resolve()
TUSD_DIR = Path(os.getenv("MUNCHY_RUNNER_SMOKE_TUSD_DIR", "runtime/tusd")).resolve()
GPU_RUNTIME_DIR = Path(
    os.getenv(
        "MUNCHY_RUNNER_SMOKE_GPU_RUNTIME_DIR",
        "../gpu-service-manager/runtime/munchy-av1-nvenc",
    )
).resolve()


class HttpResult:
    def __init__(self, status: int, body: bytes, headers: dict[str, str]) -> None:
        self.status = status
        self.body = body
        self.headers = headers

    def json(self) -> dict:
        return json.loads(self.body.decode("utf-8"))


def request(
    method: str,
    url: str,
    *,
    payload: dict | None = None,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> HttpResult:
    headers = dict(headers or {})
    body = data
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return HttpResult(resp.status, resp.read(), dict(resp.headers.items()))
    except urllib.error.HTTPError as exc:
        return HttpResult(exc.code, exc.read(), dict(exc.headers.items()))


def api(method: str, path: str, *, payload: dict | None = None, expect: int = 200) -> dict:
    result = request(method, f"{RUNNER_URL}{path}", payload=payload)
    if result.status != expect:
        body = result.body.decode("utf-8", "replace")
        raise AssertionError(
            f"{method} {path}: expected HTTP {expect}, got {result.status}: {body}"
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


def state_get(kind: str, item_id: str) -> dict | None:
    if not STATE_DB.exists():
        return None
    with sqlite3.connect(STATE_DB, timeout=30) as conn:
        row = conn.execute(
            "SELECT payload FROM states WHERE kind = ? AND id = ?",
            (kind, item_id),
        ).fetchone()
    if row is None:
        return None
    return json.loads(row[0])


def state_delete(kind: str, item_id: str) -> None:
    if not STATE_DB.exists():
        return
    with sqlite3.connect(STATE_DB, timeout=30) as conn:
        conn.execute("DELETE FROM states WHERE kind = ? AND id = ?", (kind, item_id))


def remove_upload(upload_id: str) -> None:
    payload = state_get("input-upload", upload_id)
    if payload is None:
        return
    for file_state in payload.get("files", []):
        tus_path = TUSD_DIR / str(file_state["upload_id"])
        tus_path.unlink(missing_ok=True)
        tus_path.with_suffix(tus_path.suffix + ".info").unlink(missing_ok=True)
        tus_path.with_suffix(tus_path.suffix + ".lock").unlink(missing_ok=True)
    state_delete("input-upload", upload_id)


def remove_job(job_id: str) -> None:
    state_delete("job", job_id)
    job_root = GPU_RUNTIME_DIR / "jobs" / job_id
    if job_root.exists() and job_id.startswith("smoke-restart-"):
        shutil.rmtree(job_root, ignore_errors=True)


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

if job_id.startswith("smoke-restart-"):
    shutil.rmtree(gpu_runtime_dir / "jobs" / job_id, ignore_errors=True)
"""
    subprocess.run(
        ["docker", "exec", RUNNER_CONTAINER, "python", "-c", code, job_id, upload_id],
        text=True,
        capture_output=True,
        check=True,
        timeout=120,
    )


def cleanup_smoke_state(job_id: str, upload_id: str) -> None:
    try:
        remove_job(job_id)
        remove_upload(upload_id)
    except (OSError, sqlite3.Error):
        cleanup_via_runner_container(job_id, upload_id)


def storage_hint_from_job_payload(payload: dict) -> dict:
    groups = {
        name: {
            "archive_mode": group.get("archive_mode", payload["archive_mode"]),
            "tasks": list(group.get("tasks") or []),
        }
        for name, group in dict(payload["groups"]).items()
    }
    return {
        "workflow_mode": payload.get("workflow_mode", "collection_archive"),
        "collection_archive_destination": dict(payload.get("collection_archive") or {}).get(
            "destination",
            "riverhog",
        ),
        "archive_mode": payload["archive_mode"],
        "tasks": payload["tasks"],
        "groups": groups,
    }


def create_upload(upload_id: str, rel_path: str, content: bytes, storage_hint: dict) -> None:
    digest = hashlib.sha256(content).hexdigest()
    api(
        "POST",
        "/v1/input-uploads",
        expect=201,
        payload={
            "upload_id": upload_id,
            "files": [{"path": rel_path, "bytes": len(content), "sha256": digest}],
            "storage_hint": storage_hint,
        },
    )


def complete_upload(upload_id: str, rel_path: str, content: bytes) -> None:
    upload = api("POST", f"/v1/input-uploads/{upload_id}/files/{rel_path}/upload", expect=201)
    upload_url = upload["upload_url"]
    result = request(
        "PATCH",
        upload_url,
        data=content,
        headers={
            "Tus-Resumable": "1.0.0",
            "Upload-Offset": str(upload["offset"]),
            "Content-Type": "application/offset+octet-stream",
        },
    )
    if result.status != 204:
        body = result.body.decode("utf-8", "replace")
        raise AssertionError(f"TUS PATCH expected HTTP 204, got {result.status}: {body}")
    status = api("GET", f"/v1/input-uploads/{upload_id}")
    if status["state"] != "uploaded":
        raise AssertionError(f"input upload did not complete: {status}")


def wait_job(job_id: str, predicate, *, timeout_s: float, label: str) -> dict:
    deadline = time.monotonic() + timeout_s
    last: dict | None = None
    while time.monotonic() < deadline:
        last = api("GET", f"/v1/jobs/{job_id}")
        if predicate(last):
            return last
        time.sleep(1)
    raise TimeoutError(f"timed out waiting for {label}: {last}")


def restart_runner() -> None:
    subprocess.run(
        ["docker", "restart", RUNNER_CONTAINER],
        text=True,
        capture_output=True,
        check=True,
        timeout=120,
    )


def main() -> int:
    if not STATE_DB.exists():
        raise SystemExit(f"state db does not exist: {STATE_DB}")
    ready = wait_ready()
    if ready.get("riverhog_upload_enabled"):
        raise SystemExit(
            "refusing to run restart smoke while runner Riverhog uploads are enabled; "
            "this smoke intentionally uses a disabled Riverhog handoff as its retry point"
        )
    if ready.get("scheduler_paused"):
        raise SystemExit("refusing to run restart smoke while runner scheduler is paused")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    prefix = f"smoke-restart-{stamp}-{os.getpid()}"
    upload_id = f"{prefix}-input"
    job_id = f"{prefix}-job"
    group_name = "runner-restart-smoke"
    rel_path = f"{group_name}/sample.txt"
    content = (b"munchy restart smoke\n" * 64) + prefix.encode("utf-8")
    job_payload = {
        "job_id": job_id,
        "input_upload_id": upload_id,
        "collection_slug": "runner-restart-smoke",
        "collection_timestamp": stamp,
        "workflow_mode": "collection_archive",
        "archive_mode": "originals",
        "tasks": [],
        "groups": {
            group_name: {
                "archive_mode": "originals",
                "tasks": [],
            },
        },
        "collection_archive": {
            "destination": "riverhog",
            "riverhog": {"wait": "staged"},
        },
        "notify": {"enabled": False},
    }

    try:
        create_upload(upload_id, rel_path, content, storage_hint_from_job_payload(job_payload))
        complete_upload(upload_id, rel_path, content)
        api(
            "POST",
            "/v1/jobs",
            expect=202,
            payload=job_payload,
        )
        before = wait_job(
            job_id,
            lambda job: (
                job.get("phase") == "riverhog_upload_retrying" and job.get("state") == "running"
            ),
            timeout_s=90,
            label="initial Riverhog retry",
        )
        before_attempts = int(before.get("handoff_attempts", {}).get("riverhog_upload_result") or 0)
        restart_runner()
        wait_ready()
        after = wait_job(
            job_id,
            lambda job: (
                job.get("phase") == "riverhog_upload_retrying"
                and int(job.get("handoff_attempts", {}).get("riverhog_upload_result") or 0)
                > before_attempts
            ),
            timeout_s=120,
            label="resumed Riverhog retry after runner restart",
        )
        api("POST", f"/v1/jobs/{job_id}/cancel", expect=202)
        cancelled = wait_job(
            job_id,
            lambda job: job.get("state") == "cancelled",
            timeout_s=30,
            label="safe cancellation of handoff retry",
        )
        print(
            json.dumps(
                {
                    "status": "ok",
                    "runner_url": RUNNER_URL,
                    "job": job_id,
                    "before_attempts": before_attempts,
                    "after_attempts": int(
                        after.get("handoff_attempts", {}).get("riverhog_upload_result") or 0
                    ),
                    "cancelled_at": cancelled.get("cancelled_at"),
                },
                indent=2,
            )
        )
    finally:
        try:
            api("POST", f"/v1/jobs/{job_id}/cancel", expect=202)
        except Exception:
            pass
        cleanup_smoke_state(job_id, upload_id)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise

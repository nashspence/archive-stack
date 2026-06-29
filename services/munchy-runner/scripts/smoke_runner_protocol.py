#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

RUNNER_URL = os.getenv("MUNCHY_RUNNER_SMOKE_URL", "http://127.0.0.1:8092").rstrip("/")
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
OLD_ISO = "2000-01-01T00:00:00Z"


class HttpResult:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body

    def json(self) -> dict:
        return json.loads(self.body.decode("utf-8"))


def request(
    method: str,
    url: str,
    *,
    payload: dict | None = None,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> HttpResult:
    headers = dict(headers or {})
    body = data
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
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


def state_get(kind: str, item_id: str) -> dict | None:
    with sqlite3.connect(STATE_DB, timeout=30) as conn:
        row = conn.execute(
            "SELECT payload FROM states WHERE kind = ? AND id = ?",
            (kind, item_id),
        ).fetchone()
    if row is None:
        return None
    return json.loads(row[0])


def state_set(kind: str, item_id: str, payload: dict) -> None:
    encoded = json.dumps(payload, sort_keys=True)
    with sqlite3.connect(STATE_DB, timeout=30) as conn:
        conn.execute(
            """
            INSERT INTO states(kind, id, payload, updated_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(kind, id) DO UPDATE SET
                payload = excluded.payload,
                updated_at = excluded.updated_at
            """,
            (kind, item_id, encoded, payload.get("updated_at") or ""),
        )


def state_delete(kind: str, item_id: str) -> None:
    with sqlite3.connect(STATE_DB, timeout=30) as conn:
        conn.execute("DELETE FROM states WHERE kind = ? AND id = ?", (kind, item_id))


def tus_patch(url: str, chunk: bytes, offset: int) -> int:
    result = request(
        "PATCH",
        url,
        data=chunk,
        headers={
            "Tus-Resumable": "1.0.0",
            "Upload-Offset": str(offset),
            "Content-Type": "application/offset+octet-stream",
        },
    )
    if result.status != 204:
        body = result.body.decode("utf-8", "replace")
        raise AssertionError(f"TUS PATCH expected HTTP 204, got {result.status}: {body}")
    return int(result.body.decode("utf-8") or 0) if result.body else offset + len(chunk)


def set_upload_old(upload_id: str) -> None:
    payload = state_get("input-upload", upload_id)
    if payload is None:
        raise AssertionError(f"missing input upload state: {upload_id}")
    payload["created_at"] = OLD_ISO
    payload["updated_at"] = OLD_ISO
    state_set("input-upload", upload_id, payload)
    old_ts = datetime.fromisoformat(OLD_ISO.replace("Z", "+00:00")).timestamp()
    for file_state in payload.get("files", []):
        tus_path = TUSD_DIR / str(file_state["upload_id"])
        for item in (
            tus_path,
            tus_path.with_suffix(tus_path.suffix + ".info"),
            tus_path.with_suffix(tus_path.suffix + ".lock"),
        ):
            if item.exists():
                os.utime(item, (old_ts, old_ts))


def assert_upload_missing(upload_id: str) -> None:
    result = request("GET", f"{RUNNER_URL}/v1/input-uploads/{upload_id}")
    if result.status != 404:
        raise AssertionError(
            f"expected input upload {upload_id} to be gone, got HTTP {result.status}"
        )


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
    if job_root.exists() and job_id.startswith("smoke-"):
        import shutil

        shutil.rmtree(job_root, ignore_errors=True)


def storage_hint_for(
    rel_path: str,
    *,
    workflow_mode: str = "archive",
    archive_mode: str = "originals",
    tasks: list[str] | None = None,
) -> dict:
    group_name = rel_path.split("/", 1)[0]
    tasks = list(tasks or [])
    return {
        "workflow_mode": workflow_mode,
        "archive_mode": archive_mode,
        "tasks": tasks,
        "groups": {
            group_name: {
                "archive_mode": archive_mode,
                "tasks": tasks,
            },
        },
    }


def create_upload(
    upload_id: str,
    rel_path: str,
    content: bytes,
    *,
    workflow_mode: str = "archive",
    archive_mode: str = "originals",
    tasks: list[str] | None = None,
) -> dict:
    digest = hashlib.sha256(content).hexdigest()
    return api(
        "POST",
        "/v1/input-uploads",
        expect=201,
        payload={
            "upload_id": upload_id,
            "files": [{"path": rel_path, "bytes": len(content), "sha256": digest}],
            "storage_hint": storage_hint_for(
                rel_path,
                workflow_mode=workflow_mode,
                archive_mode=archive_mode,
                tasks=tasks,
            ),
        },
    )


def complete_upload(upload_id: str, rel_path: str, content: bytes, *, partial_first: bool) -> None:
    upload = api("POST", f"/v1/input-uploads/{upload_id}/files/{rel_path}/upload", expect=201)
    upload_url = upload["upload_url"]
    if partial_first:
        split = max(1, len(content) // 2)
        tus_patch(upload_url, content[:split], 0)
        resumed = api("POST", f"/v1/input-uploads/{upload_id}/files/{rel_path}/upload", expect=201)
        if int(resumed["offset"]) != split:
            raise AssertionError(
                f"resume offset mismatch: expected {split}, got {resumed['offset']}"
            )
        tus_patch(upload_url, content[split:], split)
    else:
        tus_patch(upload_url, content, 0)

    status = api("GET", f"/v1/input-uploads/{upload_id}")
    if status["state"] != "uploaded":
        raise AssertionError(f"input upload did not complete: {status}")


def poll_job(job_id: str) -> dict:
    for _ in range(60):
        job = api("GET", f"/v1/jobs/{job_id}")
        if job.get("state") in {"succeeded", "failed"}:
            return job
        time.sleep(1)
    raise TimeoutError(f"job did not finish: {job_id}")


def poll_job_state(job_id: str, states: set[str]) -> dict:
    for _ in range(60):
        job = api("GET", f"/v1/jobs/{job_id}")
        if job.get("state") in states:
            return job
        time.sleep(1)
    raise TimeoutError(f"job did not reach {sorted(states)}: {job_id}")


def main() -> int:
    if not STATE_DIR.exists():
        raise SystemExit(f"state dir does not exist: {STATE_DIR}")
    if not STATE_DB.exists():
        raise SystemExit(f"state db does not exist: {STATE_DB}")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    prefix = f"smoke-{stamp}-{os.getpid()}"
    content = (b"munchy runner smoke\n" * 64) + prefix.encode("utf-8")
    group_name = "runner-smoke"
    rel_path = f"{group_name}/sample.txt"

    ready = api("GET", "/health/ready")
    capabilities = api("GET", "/v1/capabilities")
    workflow_modes = set(capabilities.get("workflow_modes", []))
    if "collection_preview" not in workflow_modes:
        raise AssertionError(
            f"runner capabilities did not advertise collection_preview: {capabilities}"
        )
    review_methods = set(capabilities.get("review_upload", {}).get("methods", []))
    if "rclone" not in review_methods:
        raise AssertionError(
            f"runner capabilities did not advertise rclone review upload: {capabilities}"
        )
    notify_events = set(capabilities.get("notify", {}).get("events", []))
    if "job.issue" not in notify_events:
        raise AssertionError(f"runner capabilities did not advertise notifications: {capabilities}")
    operations = capabilities.get("operations", {})
    if (
        not operations.get("cancel_job")
        or not operations.get("delete_input_upload")
        or not operations.get("pause_scheduler")
    ):
        raise AssertionError(
            f"runner capabilities did not advertise operational controls: {capabilities}"
        )
    profile_groups = capabilities.get("profile_groups", {})
    if profile_groups.get("input_path_shape") != "<profile-group>/<file>":
        raise AssertionError(
            f"runner capabilities did not advertise profile groups: {capabilities}"
        )
    storage = capabilities.get("storage", {})
    if not storage.get("input_upload_storage_hint_required"):
        raise AssertionError(
            f"runner capabilities did not advertise required storage hints: {capabilities}"
        )
    if not storage.get("eager_archive_only_encoding"):
        raise AssertionError(
            f"runner capabilities did not advertise eager archive encoding: {capabilities}"
        )

    root_upload = request(
        "POST",
        f"{RUNNER_URL}/v1/input-uploads",
        payload={
            "upload_id": f"{prefix}-bad-root",
            "files": [{"path": "sample.txt", "bytes": len(content), "sha256": None}],
            "storage_hint": storage_hint_for(rel_path),
        },
    )
    if root_upload.status != 422:
        raise AssertionError(
            "root-level input upload path should be rejected with 422, "
            f"got HTTP {root_upload.status}: {root_upload.body.decode('utf-8', 'replace')}"
        )

    upload_id = f"{prefix}-resume"
    create_upload(upload_id, rel_path, content)
    complete_upload(upload_id, rel_path, content, partial_first=True)

    abandon_id = f"{prefix}-abandon"
    create_upload(abandon_id, rel_path, content)
    abandon_upload = api(
        "POST", f"/v1/input-uploads/{abandon_id}/files/{rel_path}/upload", expect=201
    )
    tus_patch(abandon_upload["upload_url"], content[:7], 0)
    abandoned = api("DELETE", f"/v1/input-uploads/{abandon_id}", expect=202)
    if abandoned.get("state") != "deleted":
        raise AssertionError(f"input upload abandon did not report deleted: {abandoned}")
    assert_upload_missing(abandon_id)

    preupload_id = f"{prefix}-preupload"
    preupload_job_id = f"{prefix}-preupload-job"
    create_upload(preupload_id, rel_path, content)
    created_preupload_job = api(
        "POST",
        "/v1/jobs",
        expect=202,
        payload={
            "job_id": preupload_job_id,
            "input_upload_id": preupload_id,
            "collection_slug": "runner-smoke-preupload",
            "collection_timestamp": stamp,
            "archive_mode": "originals",
            "tasks": [],
            "groups": {
                group_name: {
                    "archive_mode": "originals",
                    "tasks": [],
                },
            },
            "riverhog": {"enabled": False},
            "review_upload": {"enabled": False},
            "notify": {"enabled": False},
        },
    )
    if created_preupload_job.get("input_upload_id") != preupload_id:
        raise AssertionError(f"pre-upload job did not reference upload: {created_preupload_job}")
    referenced_preupload_delete = request("DELETE", f"{RUNNER_URL}/v1/input-uploads/{preupload_id}")
    if referenced_preupload_delete.status != 409:
        raise AssertionError(
            "active referenced input upload delete should be rejected with 409, "
            f"got HTTP {referenced_preupload_delete.status}: "
            f"{referenced_preupload_delete.body.decode('utf-8', 'replace')}"
        )
    api("POST", f"/v1/jobs/{preupload_job_id}/cancel", expect=202)
    cancelled_preupload_job = poll_job_state(preupload_job_id, {"cancelled"})
    if cancelled_preupload_job.get("state") != "cancelled":
        raise AssertionError(f"pre-upload job did not cancel: {cancelled_preupload_job}")

    job_id = f"{prefix}-job"
    api(
        "POST",
        "/v1/jobs",
        expect=202,
        payload={
            "job_id": job_id,
            "input_upload_id": upload_id,
            "collection_slug": "runner-smoke",
            "collection_timestamp": stamp,
            "archive_mode": "originals",
            "tasks": [],
            "encode_profile": {
                "schema_version": 1,
                "name": "runner-smoke-profile",
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
                    "archive_mode": "originals",
                    "tasks": [],
                    "encode_profile": {
                        "schema_version": 1,
                        "name": "runner-smoke-profile",
                    },
                },
            },
            "riverhog": {"enabled": False},
            "review_upload": {
                "enabled": False,
                "method": "rclone",
                "destination": f"/tmp/{prefix}/{{collection_slug}}",
                "mode": "copy",
            },
            "notify": {
                "enabled": False,
                "recipients": ["smoke"],
            },
        },
    )
    job = poll_job(job_id)
    if job.get("state") != "succeeded":
        raise AssertionError(f"originals job failed: {job}")
    if job.get("encode_profile", {}).get("name") != "runner-smoke-profile":
        raise AssertionError(f"job did not preserve encode_profile: {job}")
    if job.get("review_upload", {}).get("method") != "rclone":
        raise AssertionError(f"job did not preserve review_upload config: {job}")
    if job.get("notify", {}).get("recipients") != ["smoke"]:
        raise AssertionError(f"job did not preserve notify config: {job}")

    preview_job_id = ""
    preview_upload_id = ""
    if ready.get("review_upload_enabled"):
        preview_upload_id = f"{prefix}-preview-upload"
        create_upload(
            preview_upload_id,
            rel_path,
            content,
            workflow_mode="collection_preview",
        )
        complete_upload(preview_upload_id, rel_path, content, partial_first=False)
        preview_job_id = f"{prefix}-preview"
        api(
            "POST",
            "/v1/jobs",
            expect=202,
            payload={
                "job_id": preview_job_id,
                "input_upload_id": preview_upload_id,
                "collection_slug": "runner-smoke-preview",
                "collection_timestamp": stamp,
                "workflow_mode": "collection_preview",
                "archive_mode": "originals",
                "tasks": [],
                "groups": {
                    group_name: {
                        "archive_mode": "originals",
                        "tasks": [],
                    },
                },
                "riverhog": {"enabled": False},
                "review_upload": {
                    "enabled": True,
                    "method": "rclone",
                    "destination": f"/tmp/{prefix}/{{collection_slug}}",
                    "mode": "copy",
                },
                "notify": {"enabled": False},
            },
        )
        preview_job = poll_job(preview_job_id)
        if preview_job.get("state") != "succeeded":
            raise AssertionError(f"collection preview job failed: {preview_job}")
        preview_result = preview_job.get("collection_preview_upload_result", {})
        if preview_result.get("method") != "rclone":
            raise AssertionError(f"collection preview did not upload with rclone: {preview_job}")
        if preview_result.get("source_label") != "collection preview":
            raise AssertionError(f"collection preview did not label upload source: {preview_job}")
        if preview_job.get("riverhog_upload_result") is not None:
            raise AssertionError(f"collection preview should not upload to Riverhog: {preview_job}")

    invalid_review = request(
        "POST",
        f"{RUNNER_URL}/v1/jobs",
        payload={
            "job_id": f"{prefix}-bad-review",
            "input_upload_id": upload_id,
            "collection_slug": "runner-smoke",
            "archive_mode": "originals",
            "tasks": [],
            "review_upload": {"enabled": True, "method": "rclone"},
        },
    )
    if invalid_review.status != 422:
        raise AssertionError(
            "rclone review upload without destination should be rejected with 422, "
            f"got HTTP {invalid_review.status}: {invalid_review.body.decode('utf-8', 'replace')}"
        )

    invalid_preview = request(
        "POST",
        f"{RUNNER_URL}/v1/jobs",
        payload={
            "job_id": f"{prefix}-bad-preview",
            "input_upload_id": upload_id,
            "collection_slug": "runner-smoke",
            "workflow_mode": "collection_preview",
            "archive_mode": "av1_nvenc",
            "tasks": ["qcut_video"],
            "review_upload": {
                "enabled": True,
                "method": "rclone",
                "destination": f"/tmp/{prefix}/{{collection_slug}}",
            },
        },
    )
    if invalid_preview.status != 422:
        raise AssertionError(
            "collection_preview without archive_video should be rejected with 422, "
            f"got HTTP {invalid_preview.status}: {invalid_preview.body.decode('utf-8', 'replace')}"
        )

    invalid_notify = request(
        "POST",
        f"{RUNNER_URL}/v1/jobs",
        payload={
            "job_id": f"{prefix}-bad-notify",
            "input_upload_id": upload_id,
            "collection_slug": "runner-smoke",
            "archive_mode": "originals",
            "tasks": [],
            "notify": {"enabled": True, "recipients": []},
        },
    )
    if invalid_notify.status != 422:
        raise AssertionError(
            "enabled notify without recipients should be rejected with 422, "
            f"got HTTP {invalid_notify.status}: {invalid_notify.body.decode('utf-8', 'replace')}"
        )

    huge = request(
        "POST",
        f"{RUNNER_URL}/v1/input-uploads",
        payload={
            "upload_id": f"{prefix}-huge",
            "files": [{"path": f"{group_name}/too-large.bin", "bytes": 10**15, "sha256": None}],
            "storage_hint": storage_hint_for(f"{group_name}/too-large.bin"),
        },
    )
    if huge.status != 507:
        body = huge.body.decode("utf-8", "replace")
        raise AssertionError(
            f"huge upload should be rejected with 507, got HTTP {huge.status}: {body}"
        )

    stale_partial_id = f"{prefix}-stale-partial"
    create_upload(stale_partial_id, rel_path, content)
    upload = api(
        "POST", f"/v1/input-uploads/{stale_partial_id}/files/{rel_path}/upload", expect=201
    )
    tus_patch(upload["upload_url"], content[:7], 0)
    set_upload_old(stale_partial_id)

    stale_orphan_id = f"{prefix}-stale-orphan"
    create_upload(stale_orphan_id, rel_path, content)
    complete_upload(stale_orphan_id, rel_path, content, partial_first=False)
    set_upload_old(stale_orphan_id)

    cleanup = api("POST", "/v1/maintenance/cleanup")
    removed = set(cleanup.get("removed", []))
    expected_removed = {
        f"input-upload:{stale_partial_id}",
        f"orphan-input-upload:{stale_orphan_id}",
    }
    if not expected_removed.issubset(removed):
        raise AssertionError(
            f"cleanup did not remove expected entries: expected {expected_removed}, got {removed}"
        )
    assert_upload_missing(stale_partial_id)
    assert_upload_missing(stale_orphan_id)

    print(
        json.dumps(
            {
                "status": "ok",
                "runner_url": RUNNER_URL,
                "resume_upload": upload_id,
                "job": job_id,
                "collection_preview_job": preview_job_id or None,
                "cleanup_removed": sorted(expected_removed),
            },
            indent=2,
        )
    )
    if preview_job_id:
        remove_job(preview_job_id)
    if preview_upload_id:
        remove_upload(preview_upload_id)
    remove_job(preupload_job_id)
    remove_upload(preupload_id)
    remove_job(job_id)
    remove_upload(upload_id)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise

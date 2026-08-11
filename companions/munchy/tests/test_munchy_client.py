from __future__ import annotations

import contextlib
import hashlib
import io
from pathlib import Path
from threading import Event
from unittest.mock import patch

import httpx
import pytest
from munchy_api_client.client import (
    CLEANUP_REQUEST_TIMEOUT_SECONDS,
    JobTerminalDuringUpload,
    MunchyAdminClient,
    MunchyClient,
    MunchyHttpError,
    RichProgressRenderer,
    SubmissionInputFile,
    SubmissionPreflightInputFile,
    SubmissionPreflightRequest,
    SubmissionUploadRequest,
    UploadProgress,
    UploadRetryReporter,
    compact_job_failure,
    format_encode_progress,
    format_handoff_progress,
    format_job_failure,
    format_job_status_line,
    format_job_summary_line,
    format_progress_status_line,
    job_finished_cleanly,
    keep_system_awake,
    progress_percent,
)


def test_admin_client_streams_and_verifies_job_diagnostic(tmp_path: Path) -> None:
    content = b"diagnostic archive"
    sha256 = hashlib.sha256(content).hexdigest()
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/diagnostic/content"):
            return httpx.Response(
                200,
                content=content,
                headers={"Content-Length": str(len(content)), "ETag": f'"{sha256}"'},
            )
        return httpx.Response(
            200,
            json={
                "job_id": "job-1",
                "created_at": "2026-08-01T00:00:00.000000Z",
                "reason": "job_failed",
                "bytes": len(content),
                "sha256": sha256,
            },
        )

    output = tmp_path / "case.tar.gz"
    with MunchyAdminClient("https://munchy.test", token="admin-token") as client:
        client._http = httpx.Client(transport=httpx.MockTransport(handle))
        receipt = client.download_job_diagnostic("job-1", output=output)

    assert output.read_bytes() == content
    assert receipt == {
        "job_id": "job-1",
        "output": str(output),
        "bytes": len(content),
        "sha256": sha256,
    }
    assert [request.url.path for request in seen] == [
        "/v1/admin/jobs/job-1/diagnostic",
        "/v1/admin/jobs/job-1/diagnostic/content",
    ]
    assert all(request.headers["Authorization"] == "Bearer admin-token" for request in seen)


def test_server_client_injects_bearer_token() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    with MunchyClient(
        "https://munchy.test",
        token="munchy-token",
        transport=httpx.MockTransport(handle),
    ) as client:
        client.request("GET", "/v1/jobs")
        client.request(
            "PATCH",
            "http://uploads.test/file",
            data=b"chunk",
            headers={"Tus-Resumable": "1.0.0"},
        )

    assert [req.headers.get("Authorization") for req in seen] == [
        "Bearer munchy-token",
        "Bearer munchy-token",
    ]


def test_keep_system_awake_uses_caffeinate_on_macos(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import munchy_api_client.client as client_module

    client_module._KEEP_AWAKE_DEPTH = 0
    client_module._KEEP_AWAKE_PROCESS = None
    calls: list[list[str]] = []

    class FakeProcess:
        terminated = False
        killed = False

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float | None = None) -> int:
            assert timeout == 2
            return 0

        def kill(self) -> None:
            self.killed = True

    fake_process = FakeProcess()

    def fake_popen(cmd: list[str], **_kwargs: object) -> FakeProcess:
        calls.append(cmd)
        return fake_process

    monkeypatch.setattr(client_module.sys, "platform", "darwin")
    monkeypatch.setattr(client_module.subprocess, "Popen", fake_popen)

    with keep_system_awake("outer"):
        with keep_system_awake("inner"):
            assert client_module._KEEP_AWAKE_DEPTH == 2

    assert calls == [["caffeinate", "-dimsu", "-w", str(client_module.os.getpid())]]
    assert fake_process.terminated is True
    assert fake_process.killed is False


def test_format_job_summary_line_includes_upload_and_encode_progress() -> None:
    line = format_job_summary_line(
        {
            "job_id": "job-1",
            "template_id": "example-q49",
            "created_at": "2026-07-27T12:34:56.987654Z",
            "state": "running",
            "phase": "eager_archive:pipeline=3/3",
            "upload_progress": {
                "files_uploaded": 10,
                "files_total": 20,
                "uploaded_bytes": 1024,
                "bytes_total": 2048,
                "percent_bytes": 50.0,
            },
            "encode_progress": {
                "files_total": 20,
                "files_encoded": 4,
                "input_bytes_total": 2048,
                "input_bytes_encoded": 512,
                "percent_input_bytes": 25.0,
                "output_bytes": 128,
                "running_batches": 1,
                "pipeline_batches": 3,
            },
        }
    )

    assert line.startswith(
        "example-q49 · 20260727T123456Z [job job-1] | job: running | eager_archive:pipeline=3/3"
    )
    assert "remote upload 10/20 files" in line
    assert "remote encode 4/20 files" in line
    assert "batches 1/3" in line


def test_format_handoff_progress_uses_expected_file_total() -> None:
    line = format_handoff_progress(
        {
            "destination": "riverhog",
            "stages": [
                {
                    "id": "transfer",
                    "label": "Riverhog Handoff",
                    "state": "open",
                    "items_done": 3,
                    "items_total": 20,
                    "item_label": "recordings",
                    "bytes_done": 1_614_000,
                    "bytes_total": 2_130_000,
                    "rate_bytes_per_second": 9_000,
                }
            ],
        }
    )

    assert line.startswith("Riverhog Handoff, open, 3/20 recordings")
    assert "1.54 MiB / 2.03 MiB" in line
    assert "8.79 KiB/s" in line


def test_format_encode_progress_distinguishes_file_and_input_byte_percent() -> None:
    line = format_encode_progress(
        {
            "files_total": 20,
            "files_encoded": 7,
            "files_encoding": 3,
            "input_bytes_total": 1000,
            "input_bytes_encoded": 213,
            "percent_files": 35.0,
            "percent_input_bytes": 21.3,
            "input_rate_bytes_per_second": 10_000,
            "output_rate_bytes_per_second": 1_000,
            "output_bytes": 2_000,
        }
    )

    assert line.startswith("remote encode 7/20 files, 35.00% files")
    assert "213 B / 1000 B input" in line
    assert "21.30% input" in line


def test_format_handoff_progress_renders_adapter_stages_in_order() -> None:
    line = format_handoff_progress(
        {
            "destination": "riverhog",
            "stages": [
                {"id": "archive", "label": "Riverhog Archive", "state": "uploading"},
                {
                    "id": "verify",
                    "label": "Riverhog Verification",
                    "items_done": 3,
                    "items_total": 10,
                },
            ],
        }
    )

    assert line == ("Riverhog Archive, uploading | Riverhog Verification, 3/10 items")


def test_format_job_summary_line_renders_review_clip_progress() -> None:
    line = format_job_summary_line(
        {
            "job_id": "job-review",
            "review": {"route_id": "camera-main-video", "profile_id": "webm-q49"},
            "state": "running",
            "phase": "review_handoff_retrying",
            "encode_progress": {
                "mode": "qcut_video",
                "phase": "encoding_clips",
                "clips_total": 48,
                "clips_done": 12,
                "clips_running": 2,
                "clips_failed": 0,
                "percent_clips": 25.0,
                "output_bytes": 1024 * 1024,
                "active_output_bytes": 512 * 1024,
                "output_rate_bytes_per_second": 256 * 1024,
            },
        }
    )

    assert "remote review 12/48 clips" in line
    assert "25.00%" in line
    assert "encoding clips" in line
    assert "2 active" in line
    assert "active output" in line
    assert "remote encode 12/48 files" not in line


def test_format_progress_status_line_renders_local_and_remote_progress() -> None:
    line = format_progress_status_line(
        {
            "local_progress": {
                "hash": {
                    "label": "hash",
                    "files_done": 4,
                    "files_total": 10,
                    "bytes_done": 400,
                    "bytes_total": 1000,
                    "percent_bytes": 40.0,
                    "rate_bytes_per_second": 200,
                    "rate_label": "logical",
                    "cache_hits": 3,
                    "cache_misses": 1,
                    "cache_writes": 1,
                },
                "preflight": {
                    "label": "preflight",
                    "files_done": 2,
                    "files_total": 10,
                    "bytes_done": 100,
                    "bytes_total": 1000,
                    "percent_bytes": 10.0,
                    "failures": 1,
                },
            },
            "upload_progress": {
                "files_uploaded": 1,
                "files_total": 10,
                "uploaded_bytes": 50,
                "bytes_total": 1000,
                "percent_bytes": 5.0,
            },
        }
    )

    assert "local hash 4/10 files" in line
    assert "cache hits 3, misses 1, writes 1" in line
    assert "local preflight 2/10 files" in line
    assert "1 failed" in line
    assert "remote upload 1/10 files" in line


def test_format_progress_status_line_shows_input_tree_when_it_lags_upload() -> None:
    line = format_progress_status_line(
        {
            "upload_progress": {
                "files_uploaded": 8,
                "files_total": 10,
                "uploaded_bytes": 800,
                "bytes_total": 1000,
                "percent_bytes": 80.0,
                "input_tree_files_ready": 5,
                "input_tree_bytes_ready": 500,
            },
        }
    )

    assert "remote upload 8/10 files" in line
    assert "remote input tree 5/10 files" in line


def test_format_progress_status_line_uses_scoped_input_tree_totals() -> None:
    line = format_progress_status_line(
        {
            "upload_progress": {
                "files_uploaded": 475,
                "files_total": 475,
                "uploaded_bytes": 1000,
                "bytes_total": 1000,
                "percent_bytes": 100.0,
                "input_tree_files_ready": 0,
                "input_tree_files_total": 194,
                "input_tree_bytes_ready": 0,
                "input_tree_bytes_total": 400,
            },
            "encode_progress": {
                "mode": "eager_archive",
                "files_total": 68,
                "files_encoding": 4,
                "input_bytes_total": 600,
                "input_bytes_encoding": 100,
                "running_batches": 1,
                "pipeline_batches": 3,
            },
        }
    )

    assert "remote upload 475/475 files" in line
    assert "remote input tree 0/194 files" in line
    assert "remote input tree 0/475 files" not in line
    assert "remote encode 0/68 files" in line


def test_progress_percent_uses_uploaded_bytes_when_payload_percent_is_stale() -> None:
    progress = {
        "files_uploaded": 147,
        "files_total": 3638,
        "uploaded_bytes": 7 * 1024 * 1024 * 1024,
        "bytes_total": 63 * 1024 * 1024 * 1024,
        "percent_bytes": 0.0,
    }

    assert progress_percent(progress, percent_key="percent_bytes") == pytest.approx(
        11.111,
        rel=0.001,
    )


def test_progress_percent_prefers_uploaded_bytes_when_payload_percent_is_stale_positive() -> None:
    progress = {
        "uploaded_bytes": 46,
        "bytes_total": 100,
        "percent_bytes": 57.0,
    }

    assert progress_percent(progress, percent_key="percent_bytes") == pytest.approx(46.0)


def test_upload_retry_reporter_uses_live_renderer_for_transient_issues() -> None:
    updates: list[dict[str, object]] = []

    class Renderer:
        is_live = True

        def update(self, job: dict[str, object], *, force: bool = False) -> None:
            updates.append(job)

    reporter = UploadRetryReporter(label="remote upload", renderer=Renderer())  # type: ignore[arg-type]
    stderr = io.StringIO()

    with contextlib.redirect_stderr(stderr):
        reporter.mark_retry(
            rel_path="camera/clip.mp4",
            retry_count=1,
            retry_delay=2.0,
            exc=ConnectionResetError(54, "Connection reset by peer"),
        )
        reporter.finish()

    assert stderr.getvalue() == ""
    assert updates[0]["transient_issue"]["label"] == "remote upload"  # type: ignore[index]
    assert updates[0]["transient_issue"]["next_retry_seconds"] == 2.0  # type: ignore[index]
    assert updates[-1]["transient_issue"]["message"] == "recovered from transient issues"  # type: ignore[index]


def test_upload_retry_reporter_does_not_restart_stopped_live_renderer() -> None:
    class Renderer:
        is_live = True
        started = True
        updates = 0

        def update(self, job: dict[str, object], *, force: bool = False) -> None:
            if not self.started:
                raise AssertionError("stopped live renderer should not be updated")
            self.updates += 1

    renderer = Renderer()
    reporter = UploadRetryReporter(label="remote upload", renderer=renderer)  # type: ignore[arg-type]
    reporter.mark_retry(
        rel_path="camera/clip.mp4",
        retry_count=1,
        retry_delay=2.0,
        exc=ConnectionResetError(54, "Connection reset by peer"),
    )
    renderer.started = False
    reporter.finish()
    reporter.finish()

    assert renderer.updates == 1
    assert reporter.total_retries == 0
    assert reporter.files == set()


def test_rich_renderer_uses_transient_live_display() -> None:
    pytest.importorskip("rich")

    renderer = RichProgressRenderer(include_job=False, title="Test")

    assert renderer.live.transient is True


def test_rich_renderer_overlays_transient_issue_without_replacing_progress() -> None:
    pytest.importorskip("rich")

    class Live:
        def __init__(self) -> None:
            self.rendered: list[object] = []

        def update(self, renderable: object, *, refresh: bool = False) -> None:
            self.rendered.append(renderable)

    live = Live()
    renderer = RichProgressRenderer(include_job=True, title="Test")
    renderer.started = True
    renderer.live = live  # type: ignore[assignment]
    renderer.update(
        {
            "state": "running",
            "phase": "gpu:camera",
            "upload_progress": {
                "files_uploaded": 5,
                "files_total": 10,
                "uploaded_bytes": 500,
                "bytes_total": 1000,
                "percent_bytes": 50.0,
            },
        }
    )
    renderer.update(
        {
            "transient_issue": {
                "label": "remote job status",
                "retries": 1,
                "next_retry_seconds": 2.0,
                "error": "connection reset",
            }
        }
    )

    assert renderer.current_job["state"] == "running"
    assert renderer.current_job["phase"] == "gpu:camera"
    assert renderer.current_job["upload_progress"]["files_uploaded"] == 5
    assert renderer.transient_issue is not None
    assert renderer.transient_issue["label"] == "remote job status"
    assert len(live.rendered) == 2


def test_rich_renderer_reserves_single_line_for_transient_issues() -> None:
    pytest.importorskip("rich")
    from rich.console import Console

    renderer = RichProgressRenderer(include_job=True, title="Test")
    base_job = {
        "state": "running",
        "phase": "gpu:camera",
        "upload_progress": {
            "files_uploaded": 5,
            "files_total": 10,
            "uploaded_bytes": 500,
            "bytes_total": 1000,
            "percent_bytes": 50.0,
        },
    }

    def render_lines(job: dict[str, object]) -> list[str]:
        console = Console(record=True, width=72, color_system=None)
        console.print(renderer._render(job))
        return console.export_text().splitlines()

    without_issue = render_lines(base_job)
    with_issue = render_lines(
        {
            **base_job,
            "transient_issue": {
                "label": "remote job status",
                "retries": 12,
                "files": 3,
                "next_retry_seconds": 60.0,
                "error": "connection reset by peer while reading a very long response body",
                "path": "camera/" + ("very-long-directory/" * 8) + "clip.mp4",
            },
        }
    )

    assert len(with_issue) == len(without_issue)
    assert not any("Transient Issue" in line for line in without_issue)
    assert any("Remote Job Status Issue" in line for line in with_issue)
    assert all(len(line) <= 72 for line in with_issue)


def test_rich_renderer_clears_transient_issue_when_upload_advances() -> None:
    pytest.importorskip("rich")

    class Live:
        def __init__(self) -> None:
            self.rendered: list[object] = []

        def update(self, renderable: object, *, refresh: bool = False) -> None:
            self.rendered.append(renderable)

    live = Live()
    renderer = RichProgressRenderer(include_job=False, title="Test")
    renderer.started = True
    renderer.live = live  # type: ignore[assignment]
    renderer.update(
        {
            "upload_progress": {
                "files_uploaded": 1,
                "files_total": 10,
                "uploaded_bytes": 100,
                "bytes_total": 1000,
            }
        }
    )
    renderer.update({"transient_issue": {"label": "remote upload", "retries": 1}})

    assert renderer.transient_issue is not None

    renderer.update(
        {
            "upload_progress": {
                "files_uploaded": 2,
                "files_total": 10,
                "uploaded_bytes": 200,
                "bytes_total": 1000,
            }
        }
    )

    assert renderer.transient_issue is None


def test_rich_renderer_uses_adapter_supplied_handoff_rows() -> None:
    pytest.importorskip("rich")
    from rich.console import Console

    renderer = RichProgressRenderer(include_job=True, title="Test")
    job = {
        "state": "running",
        "phase": "eager_archive:pipeline=3/3",
        "handoff_progress": {
            "destination": "riverhog",
            "stages": [
                {
                    "id": "archive",
                    "label": "Riverhog Archive",
                    "state": "waiting",
                }
            ],
        },
    }
    console = Console(record=True, width=100, color_system=None)

    console.print(renderer._render(job))
    text = console.export_text()

    assert "Riverhog Archive" in text
    assert "waiting" in text


def test_format_job_summary_line_includes_encoder_queue_position() -> None:
    line = format_job_summary_line(
        {
            "job_id": "job-2",
            "template_id": "example-q49",
            "created_at": "2026-07-27T12:34:56Z",
            "state": "queued",
            "phase": "queued",
            "queue": {
                "position": 2,
                "running_job_limit": 1,
                "running_jobs": 1,
                "scheduled_jobs": 0,
            },
        }
    )

    assert "job: queued" in line
    assert "encoder queue position 2 (1/1 running or starting)" in line


def test_format_job_status_line_includes_cleanup_completion() -> None:
    line = format_job_status_line(
        {
            "state": "failed",
            "phase": "gpu",
            "cleanup_completed_at": "2026-01-01T00:00:00Z",
            "cleanup_removed": ["job-work:job-1", "input-upload:upload-1"],
        }
    )

    assert "cleanup complete (2 item(s) removed)" in line


def test_format_job_status_line_uses_compacted_cleanup_count() -> None:
    line = format_job_status_line(
        {
            "state": "succeeded",
            "phase": "done",
            "cleanup_completed_at": "2026-01-01T00:00:00Z",
            "cleanup_removed_count": 164,
        }
    )

    assert "cleanup complete (164 item(s) removed)" in line


def test_format_job_failure_is_compact_and_includes_error_details() -> None:
    failure = format_job_failure(
        {
            "job_id": "job-1",
            "template_id": "camera-archive",
            "created_at": "2026-07-27T12:34:56Z",
            "state": "failed",
            "phase": "handoff",
            "error": "rclone failed after many retries",
            "gpu_statuses": {
                "batch-1": {
                    "state": "failed",
                    "error": "ffmpeg failed for camera/clip.mp4",
                    "large": ["payload"] * 100,
                }
            },
            "eager_archive": {"large": ["payload"] * 100},
        },
        label="review job",
    )

    assert failure.startswith("review job did not succeed:")
    assert "- job: job-1" in failure
    assert "- identity: camera-archive · 20260727T123456Z" in failure
    assert "- status: job: failed" in failure
    assert "- error: rclone failed after many retries" in failure
    assert "- gpu statuses.batch-1.error: ffmpeg failed for camera/clip.mp4" in failure
    assert "eager_archive" not in failure
    assert len(failure) < 700


def test_compact_job_failure_is_single_line_and_prefers_root_error() -> None:
    failure = compact_job_failure(
        {
            "job_id": "job-1",
            "state": "failed",
            "phase": "routing",
            "error": "routing failed for camera/clip.wav: no matching route",
            "gpu_statuses": {"large": ["payload"] * 100},
        }
    )

    assert failure == ("Munchy job failed: routing failed for camera/clip.wav: no matching route")
    assert "\n" not in failure


def test_compact_job_failure_falls_back_to_state_and_phase() -> None:
    assert compact_job_failure({"state": "canceled", "phase": "handoff"}) == (
        "Munchy job did not finish safely (canceled during handoff)"
    )


def test_server_client_list_jobs_validates_response() -> None:
    client = MunchyClient("https://munchy.test")

    def fake_json(method: str, path: str, **_kwargs: object) -> dict[str, object]:
        assert method == "GET"
        assert path == (
            "/v1/jobs?page=2&per_page=5&sort=created_at&order=asc&terminal=all"
            "&all=true"
            "&q=camera&state=running&workflow_mode=collection_archive"
            "&handoff_destination=riverhog"
            "&cancel_requested=false&storage_wait=true"
        )
        return {
            "page": 2,
            "per_page": 5,
            "total": 1,
            "jobs": [{"job_id": "job-1"}, "ignored"],
        }

    client.json = fake_json  # type: ignore[method-assign]

    assert client.list_jobs(
        page=2,
        per_page=5,
        sort="created_at",
        order="asc",
        query="camera",
        terminal="all",
        state="running",
        workflow_mode="collection_archive",
        handoff_destination="riverhog",
        cancel_requested=False,
        storage_wait=True,
        all_items=True,
    ) == {
        "page": 2,
        "per_page": 5,
        "total": 1,
        "jobs": [{"job_id": "job-1"}],
    }


def test_wait_for_job_polls_compact_status() -> None:
    client = MunchyClient("https://munchy.test")
    calls: list[bool] = []

    def fake_get_job(job_id: str, *, compact: bool = False) -> dict[str, object]:
        calls.append(compact)
        return {
            "job_id": job_id,
            "state": "succeeded",
            "phase": "done",
            "handoff": {"state": "complete", "safe_to_delete": True},
        }

    client.get_job = fake_get_job  # type: ignore[method-assign]

    assert client.wait_for_job("job-1", interval=0)["state"] == "succeeded"
    assert calls == [True]


def test_upload_progress_polls_bounded_compact_job_status() -> None:
    client = MunchyClient("https://munchy.test")
    request = SubmissionUploadRequest(
        submission_id="submission-1",
        template_id="test-template",
        files=(),
    )
    calls: list[tuple[str, bool]] = []

    def fake_get_job(job_id: str, *, compact: bool = False) -> dict[str, object]:
        calls.append((job_id, compact))
        return {"job_id": job_id, "state": "uploading", "phase": "waiting_for_upload"}

    client.get_job = fake_get_job  # type: ignore[method-assign]

    assert client._job_status(request)["state"] == "uploading"
    assert calls == [("submission-1", True)]


def test_wait_for_job_continues_until_handoff_is_safe_to_delete() -> None:
    client = MunchyClient("https://munchy.test")
    responses: list[dict[str, object]] = [
        {
            "job_id": "job-1",
            "state": "succeeded",
            "handoff": {"state": "transferring", "safe_to_delete": False},
            "handoff_progress": {
                "external_id": 42,
                "state": "archiving",
                "safe_to_delete": False,
            },
        },
        {
            "job_id": "job-1",
            "state": "succeeded",
            "handoff": {"state": "complete", "safe_to_delete": True},
            "handoff_progress": {
                "external_id": 42,
                "state": "finalized",
                "safe_to_delete": True,
            },
        },
    ]

    def fake_get_job(job_id: str, *, compact: bool = False) -> dict[str, object]:
        assert compact is True
        assert job_id == "job-1"
        return responses.pop(0)

    client.get_job = fake_get_job  # type: ignore[method-assign]

    final = client.wait_for_job("job-1", interval=0)

    assert job_finished_cleanly(final)
    assert responses == []


def test_job_finished_cleanly_requires_safe_handoff() -> None:
    assert not job_finished_cleanly(
        {
            "state": "succeeded",
            "handoff": {"state": "transferring", "safe_to_delete": False},
            "handoff_progress": {
                "external_id": 42,
                "state": "archiving",
                "safe_to_delete": False,
            },
        }
    )


def test_cancel_job_cleanup_uses_long_timeout() -> None:
    client = MunchyClient("https://munchy.test")
    seen: dict[str, object] = {}

    def fake_json(
        method: str,
        path: str,
        **kwargs: object,
    ) -> dict[str, object]:
        seen["method"] = method
        seen["path"] = path
        seen["timeout"] = kwargs.get("timeout")
        seen["expect"] = kwargs.get("expect")
        return {"job_id": "job-1", "state": "canceled"}

    client.json = fake_json  # type: ignore[method-assign]

    client.cancel_job("job-1", cleanup=True)

    assert seen == {
        "method": "POST",
        "path": "/v1/jobs/job-1/cancel?cleanup=true",
        "timeout": CLEANUP_REQUEST_TIMEOUT_SECONDS,
        "expect": {202},
    }


def test_submission_preflight_failure_uses_submission_scoped_endpoint() -> None:
    client = MunchyClient("https://munchy.test")
    seen: dict[str, object] = {}
    payload = {
        "submission_id": "submission-1",
        "message": "local validation failed",
    }

    def fake_json(method: str, path: str, **kwargs: object) -> dict[str, object]:
        seen.update(method=method, path=path, **kwargs)
        return {"status": "recorded"}

    client.json = fake_json  # type: ignore[method-assign]

    result = client.record_submission_preflight_failure(payload)

    assert result == {"status": "recorded"}
    assert seen == {
        "method": "POST",
        "path": "/v1/submissions/preflight-failures",
        "payload": payload,
        "expect": {202},
    }


def test_submission_preflight_sends_only_admission_facts() -> None:
    client = MunchyClient("https://munchy.test")
    seen: dict[str, object] = {}
    request = SubmissionPreflightRequest(
        template_id="camera-archive",
        inputs={"route": "camera-main"},
        run_id="20260101T000000.123456Z",
        files=(
            SubmissionPreflightInputFile(
                rel_path="camera/clip.mp4",
                bytes=123,
            ),
        ),
    )

    def fake_retry(method: str, path: str, **kwargs: object) -> dict[str, object]:
        seen.update(method=method, path=path, **kwargs)
        return {"accepted": True}

    client._json_with_transient_retries = fake_retry  # type: ignore[method-assign]

    assert client.preflight_submission(request) == {"accepted": True}
    assert seen == {
        "method": "POST",
        "path": "/v1/submissions/preflight",
        "payload": {
            "template_id": "camera-archive",
            "inputs": {"route": "camera-main"},
            "files": [{"path": "camera/clip.mp4", "bytes": 123}],
            "handoff_on_failure": "preserve_for_resume",
            "run_id": "20260101T000000.123456Z",
        },
        "label": "submission preflight",
        "timeout": 300.0,
    }


def test_admin_client_controls_scheduler_through_admin_endpoints() -> None:
    client = MunchyAdminClient("https://munchy.test", token="admin-token")
    seen: list[tuple[str, str]] = []

    def fake_json(method: str, path: str, **_kwargs: object) -> dict[str, object]:
        seen.append((method, path))
        return {"paused": path.endswith("pause")}

    client.json = fake_json  # type: ignore[method-assign]

    assert client.get_scheduler_status() == {"paused": False}
    assert client.pause_scheduler() == {"paused": True}
    assert client.resume_scheduler() == {"paused": False}
    assert seen == [
        ("GET", "/v1/admin/scheduler"),
        ("POST", "/v1/admin/scheduler/pause"),
        ("POST", "/v1/admin/scheduler/resume"),
    ]


def test_resume_job_posts_server_resume_endpoint() -> None:
    client = MunchyClient("https://munchy.test")
    seen: dict[str, object] = {}

    def fake_json(
        method: str,
        path: str,
        **kwargs: object,
    ) -> dict[str, object]:
        seen["method"] = method
        seen["path"] = path
        seen["expect"] = kwargs.get("expect")
        return {"job_id": "job-1", "state": "queued"}

    client.json = fake_json  # type: ignore[method-assign]

    resumed = client.resume_job("job-1")

    assert resumed == {"job_id": "job-1", "state": "queued"}
    assert seen == {
        "method": "POST",
        "path": "/v1/jobs/job-1/resume",
        "expect": {202},
    }


def test_munchy_http_error_formats_insufficient_storage_concisely() -> None:
    error = MunchyHttpError(
        "POST",
        "https://munchy.test/v1/submissions",
        507,
        b"""
        {
          "error": {
            "code": "insufficient_storage",
            "message": "storage capacity is insufficient",
            "details": {
              "label": "source upload spool, future gpu scratch",
              "required_bytes": 2147483648,
              "free_bytes": 1073741824,
              "reserved_bytes": 536870912
            }
          }
        }
        """,
    )

    assert "insufficient storage for source upload spool, future gpu scratch" in str(error)
    assert "need 2.00 GiB free" in str(error)
    assert "have 1.00 GiB" in str(error)
    assert "512.00 MiB reserved by active uploads" in str(error)


def test_upload_file_retries_transient_error_and_refreshes_offset(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    item = SubmissionInputFile(
        source=source,
        rel_path="video/clip.mp4",
        bytes=source.stat().st_size,
        sha256="0" * 64,
    )

    client = MunchyClient("https://munchy.test")
    json_calls = 0
    patch_calls = 0

    def fake_json(method: str, path: str, **_kwargs: object) -> dict[str, object]:
        nonlocal json_calls
        assert method == "POST"
        json_calls += 1
        offset = 0 if json_calls == 1 else item.bytes
        return {
            "upload_url": "http://uploads.test/file",
            "offset": offset,
            "length": item.bytes,
        }

    def fake_patch(upload_url: str, *, offset: int, content: bytes) -> int:
        nonlocal patch_calls
        assert upload_url == "http://uploads.test/file"
        assert offset == 0
        assert content == b"video"
        patch_calls += 1
        raise ConnectionResetError(54, "Connection reset by peer")

    client.json = fake_json  # type: ignore[method-assign]
    client._patch_upload_chunk = fake_patch  # type: ignore[method-assign]

    with patch("munchy_api_client.client.time.sleep"):
        with contextlib.redirect_stderr(io.StringIO()):
            client.upload_file("upload", item, chunk_bytes=1024)

    assert json_calls == 2
    assert patch_calls == 1


def test_upload_file_retries_temporary_insufficient_storage(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    item = SubmissionInputFile(
        source=source,
        rel_path="video/clip.mp4",
        bytes=source.stat().st_size,
        sha256="0" * 64,
    )

    client = MunchyClient("https://munchy.test")
    json_calls = 0
    patch_calls = 0

    def fake_json(method: str, path: str, **_kwargs: object) -> dict[str, object]:
        nonlocal json_calls
        assert method == "POST"
        json_calls += 1
        offset = 0 if json_calls == 1 else item.bytes
        return {
            "upload_url": "http://uploads.test/file",
            "offset": offset,
            "length": item.bytes,
        }

    def fake_patch(upload_url: str, *, offset: int, content: bytes) -> int:
        nonlocal patch_calls
        assert offset == 0
        assert content == b"video"
        patch_calls += 1
        raise MunchyHttpError("PATCH", upload_url, 507, b"temporary storage pressure")

    client.json = fake_json  # type: ignore[method-assign]
    client._patch_upload_chunk = fake_patch  # type: ignore[method-assign]

    with patch("munchy_api_client.client.time.sleep"):
        with contextlib.redirect_stderr(io.StringIO()):
            client.upload_file("upload", item, chunk_bytes=1024)

    assert json_calls == 2
    assert patch_calls == 1


def test_upload_file_retries_tusd_interrupted_request(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    item = SubmissionInputFile(
        source=source,
        rel_path="video/clip.mp4",
        bytes=source.stat().st_size,
        sha256="0" * 64,
    )

    client = MunchyClient("https://munchy.test")
    json_calls = 0
    patch_calls = 0

    def fake_json(method: str, path: str, **_kwargs: object) -> dict[str, object]:
        nonlocal json_calls
        assert method == "POST"
        json_calls += 1
        offset = 0 if json_calls == 1 else item.bytes
        return {
            "upload_url": "http://uploads.test/file",
            "offset": offset,
            "length": item.bytes,
        }

    def fake_patch(upload_url: str, *, offset: int, content: bytes) -> int:
        nonlocal patch_calls
        assert offset == 0
        assert content == b"video"
        patch_calls += 1
        raise MunchyHttpError(
            "PATCH",
            upload_url,
            400,
            b"ERR_UPLOAD_INTERRUPTED: upload has been interrupted by another request",
        )

    client.json = fake_json  # type: ignore[method-assign]
    client._patch_upload_chunk = fake_patch  # type: ignore[method-assign]

    with patch("munchy_api_client.client.time.sleep"):
        with contextlib.redirect_stderr(io.StringIO()):
            client.upload_file("upload", item, chunk_bytes=1024)

    assert json_calls == 2
    assert patch_calls == 1


def test_upload_file_reports_canceled_job_after_tusd_cleanup(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    item = SubmissionInputFile(
        source=source,
        rel_path="video/clip.mp4",
        bytes=source.stat().st_size,
        sha256="0" * 64,
    )
    stop_event = Event()
    client = MunchyClient("https://munchy.test")

    def fake_json(method: str, path: str, **_kwargs: object) -> dict[str, object]:
        if method == "POST":
            return {
                "upload_url": "http://uploads.test/file",
                "offset": 0,
                "length": item.bytes,
            }
        assert method == "GET"
        return {
            "job": {
                "job_id": "job-1",
                "state": "canceled",
                "phase": "canceled",
            }
        }

    def fake_patch(upload_url: str, *, offset: int, content: bytes) -> int:
        assert offset == 0
        assert content == b"video"
        raise MunchyHttpError(
            "PATCH",
            upload_url,
            404,
            b"ERR_UPLOAD_NOT_FOUND: upload not found",
        )

    client.json = fake_json  # type: ignore[method-assign]
    client._patch_upload_chunk = fake_patch  # type: ignore[method-assign]

    with pytest.raises(JobTerminalDuringUpload) as exc_info:
        client.upload_file(
            "submission-1",
            item,
            chunk_bytes=1024,
            stop_event=stop_event,
        )

    assert stop_event.is_set()
    assert "canceled" in str(exc_info.value)


def test_upload_file_does_not_retry_non_transient_http_error(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    item = SubmissionInputFile(
        source=source,
        rel_path="video/clip.mp4",
        bytes=source.stat().st_size,
        sha256="0" * 64,
    )
    client = MunchyClient("https://munchy.test")

    def fake_json(method: str, path: str, **_kwargs: object) -> dict[str, object]:
        return {
            "upload_url": "http://uploads.test/file",
            "offset": 0,
            "length": item.bytes,
        }

    def fake_patch(upload_url: str, *, offset: int, content: bytes) -> int:
        assert offset == 0
        assert content == b"video"
        raise MunchyHttpError("PATCH", upload_url, 400, b"bad request")

    client.json = fake_json  # type: ignore[method-assign]
    client._patch_upload_chunk = fake_patch  # type: ignore[method-assign]

    with pytest.raises(MunchyHttpError):
        client.upload_file("upload", item, chunk_bytes=1024)


def test_upload_file_reports_successful_tus_chunk_offsets(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"abcdefgh")
    item = SubmissionInputFile(
        source=source,
        rel_path="video/clip.mp4",
        bytes=source.stat().st_size,
        sha256="0" * 64,
    )
    client = MunchyClient("https://munchy.test")
    offsets: list[int] = []

    def fake_json(method: str, path: str, **_kwargs: object) -> dict[str, object]:
        assert method == "POST"
        assert path == "/v1/submissions/upload/files/video/clip.mp4/upload"
        return {
            "upload_url": "http://uploads.test/file",
            "offset": 0,
            "length": item.bytes,
        }

    def fake_patch(upload_url: str, *, offset: int, content: bytes) -> int:
        assert upload_url == "http://uploads.test/file"
        return offset + len(content)

    client.json = fake_json  # type: ignore[method-assign]
    client._patch_upload_chunk = fake_patch  # type: ignore[method-assign]

    client.upload_file(
        "upload",
        item,
        chunk_bytes=4,
        progress_callback=lambda _uploaded_item, offset: offsets.append(offset),
    )

    assert offsets == [4, 8]


def test_upload_files_resumes_each_file_from_its_server_offset(tmp_path: Path) -> None:
    files = []
    for index in range(3):
        source = tmp_path / f"clip{index}.mp4"
        source.write_bytes(b"video")
        files.append(
            SubmissionInputFile(
                source=source,
                rel_path=f"video/clip{index}.mp4",
                bytes=source.stat().st_size,
                sha256=str(index) * 64,
            )
        )
    request = SubmissionUploadRequest(
        submission_id="submission-1",
        template_id="test-template",
        files=tuple(files),
        upload_workers=3,
        upload_chunk_mib=9,
    )
    client = MunchyClient("https://munchy.test")
    uploaded: list[str] = []

    def fake_upload_file(
        upload_id: str,
        item: SubmissionInputFile,
        *,
        chunk_bytes: int,
        retry_reporter: object | None = None,
        stop_event: object | None = None,
        progress_callback: object | None = None,
    ) -> None:
        uploaded.append(item.rel_path)

    client.upload_file = fake_upload_file  # type: ignore[method-assign]

    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        upload = client.upload_files(request)

    assert sorted(uploaded) == sorted(item.rel_path for item in files)
    assert upload == {
        "state": "uploaded",
        "files_total": 3,
        "files_uploaded": 3,
        "bytes_total": 15,
        "uploaded_bytes": 15,
    }
    assert "with 3 workers, 9 MiB chunks" in stderr.getvalue()


def test_upload_files_publishes_exact_provenance_before_payload(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    journal_id = "urn:uuid:00000000-0000-4000-8000-000000000041"
    state_id = "urn:uuid:00000000-0000-4000-8000-000000000042"
    journal = b'\x1e{"exact":"journal"}\n'
    item = SubmissionInputFile(
        source=source,
        rel_path="video/clip.mp4",
        bytes=source.stat().st_size,
        sha256=hashlib.sha256(b"video").hexdigest(),
        provenance={
            "status": "captured",
            "journal_id": journal_id,
            "current_state_id": state_id,
        },
        provenance_journals={journal_id: journal},
    )
    request = SubmissionUploadRequest(
        submission_id="submission-provenance",
        template_id="test-template",
        files=(item,),
        upload_workers=1,
        upload_chunk_mib=9,
    )
    client = MunchyClient("https://munchy.test")
    calls: list[str] = []

    def put_journal(
        submission_id: str,
        requested_journal_id: str,
        *,
        content: bytes,
        sha256: str,
    ) -> dict[str, object]:
        assert submission_id == request.submission_id
        assert requested_journal_id == journal_id
        assert content == journal
        assert sha256 == hashlib.sha256(journal).hexdigest()
        calls.append("provenance")
        return {"journal_id": journal_id}

    def upload_file(
        upload_id: str,
        requested: SubmissionInputFile,
        **_kwargs: object,
    ) -> None:
        assert upload_id == request.submission_id
        assert requested is item
        calls.append("payload")

    client.put_submission_provenance_journal = put_journal  # type: ignore[method-assign]
    client.upload_file = upload_file  # type: ignore[method-assign]

    upload = client.upload_files(request)

    assert calls == ["provenance", "payload"]
    assert upload["state"] == "uploaded"


def test_upload_files_tolerates_compact_status_timeout(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    file = SubmissionInputFile(
        source=source,
        rel_path="video/clip.mp4",
        bytes=source.stat().st_size,
        sha256="0" * 64,
    )
    request = SubmissionUploadRequest(
        submission_id="submission-1",
        template_id="test-template",
        files=(file,),
        upload_workers=1,
        upload_chunk_mib=9,
    )
    client = MunchyClient("https://munchy.test")
    uploaded: list[str] = []
    job_gets = 0

    def fake_upload_file(
        upload_id: str,
        item: SubmissionInputFile,
        *,
        chunk_bytes: int,
        retry_reporter: object | None = None,
        stop_event: object | None = None,
        progress_callback: object | None = None,
    ) -> None:
        uploaded.append(item.rel_path)

    def fake_get_job(job_id: str, *, compact: bool = False) -> dict[str, object]:
        nonlocal job_gets
        assert job_id == "submission-1"
        assert compact is True
        job_gets += 1
        raise TimeoutError("timed out")

    client.upload_file = fake_upload_file  # type: ignore[method-assign]
    client.get_job = fake_get_job  # type: ignore[method-assign]

    with patch("munchy_api_client.client.time.sleep"):
        upload = client.upload_files(request)

    assert uploaded == [file.rel_path]
    assert upload["state"] == "uploaded"
    assert job_gets == 1


def test_upload_progress_can_merge_remote_upload_and_encode_progress() -> None:
    item = SubmissionInputFile(
        source=Path("clip.mp4"),
        rel_path="video/clip.mp4",
        bytes=4,
        sha256="0" * 64,
    )
    renderer_jobs: list[dict[str, object]] = []

    class Renderer:
        def update(self, job: dict[str, object], *, force: bool = False) -> None:
            renderer_jobs.append(job)

    progress = UploadProgress(
        total_files=1,
        total_bytes=4,
        renderer=Renderer(),  # type: ignore[arg-type]
        job_status_provider=lambda: {
            "upload_progress": {
                "files_uploaded": 1,
                "files_total": 1,
                "uploaded_bytes": 4,
                "bytes_total": 4,
            },
            "encode_progress": {
                "files_total": 1,
                "files_encoded": 1,
                "input_bytes_total": 4,
                "input_bytes_encoded": 4,
            },
        },
    )

    progress.mark_complete(item)

    assert renderer_jobs
    assert "upload_progress" in renderer_jobs[0]
    assert "encode_progress" in renderer_jobs[0]


def test_upload_progress_renders_accepted_chunk_bytes_before_file_completion() -> None:
    item = SubmissionInputFile(
        source=Path("clip.mp4"),
        rel_path="video/clip.mp4",
        bytes=100,
        sha256="0" * 64,
    )
    renderer_jobs: list[dict[str, object]] = []

    class Renderer:
        def update(self, job: dict[str, object], *, force: bool = False) -> None:
            renderer_jobs.append(job)

    progress = UploadProgress(
        total_files=1,
        total_bytes=100,
        renderer=Renderer(),  # type: ignore[arg-type]
        job_status_provider=lambda: {
            "upload_progress": {
                "files_uploaded": 0,
                "files_total": 1,
                "uploaded_bytes": 0,
                "bytes_total": 100,
            },
        },
    )

    progress.mark_uploaded(item, 25)

    upload = renderer_jobs[0]["upload_progress"]
    assert upload["files_uploaded"] == 0  # type: ignore[index]
    assert upload["files_total"] == 1  # type: ignore[index]
    assert upload["uploaded_bytes"] == 25  # type: ignore[index]
    assert upload["bytes_total"] == 100  # type: ignore[index]


def test_live_upload_progress_does_not_poll_remote_status_at_render_rate() -> None:
    item = SubmissionInputFile(
        source=Path("clip.mp4"),
        rel_path="video/clip.mp4",
        bytes=100,
        sha256="0" * 64,
    )
    renderer_jobs: list[dict[str, object]] = []

    class Renderer:
        is_live = True

        def update(self, job: dict[str, object], *, force: bool = False) -> None:
            renderer_jobs.append(job)

    provider_calls = 0

    def job_status_provider() -> dict[str, object]:
        nonlocal provider_calls
        provider_calls += 1
        return {
            "upload_progress": {
                "files_uploaded": 0,
                "files_total": 1,
                "uploaded_bytes": 0,
                "bytes_total": 100,
            },
        }

    ticks = iter([100.0, 100.1, 101.2])
    with patch("munchy_api_client.client.time.monotonic", side_effect=lambda: next(ticks)):
        progress = UploadProgress(
            total_files=1,
            total_bytes=100,
            renderer=Renderer(),  # type: ignore[arg-type]
            job_status_provider=job_status_provider,
        )
        progress.mark_uploaded(item, 10)
        progress.mark_uploaded(item, 20)

    assert len(renderer_jobs) == 2
    assert provider_calls == 1
    assert renderer_jobs[-1]["upload_progress"]["uploaded_bytes"] == 20  # type: ignore[index]


def test_upload_progress_uses_one_remote_sample_per_tick_for_rate() -> None:
    item = SubmissionInputFile(
        source=Path("clip.mp4"),
        rel_path="video/clip.mp4",
        bytes=20,
        sha256="0" * 64,
    )
    renderer_jobs: list[dict[str, object]] = []

    class Renderer:
        def update(self, job: dict[str, object], *, force: bool = False) -> None:
            renderer_jobs.append(job)

    progress = UploadProgress(
        total_files=10,
        total_bytes=100,
        completed_files=1,
        completed_bytes=20,
        renderer=Renderer(),  # type: ignore[arg-type]
        job_status_provider=lambda: {
            "upload_progress": {
                "files_uploaded": 5,
                "files_total": 10,
                "uploaded_bytes": 60,
                "bytes_total": 100,
            },
        },
    )
    progress.last_printed_at -= 20

    progress.mark_complete(item)

    upload = renderer_jobs[0]["upload_progress"]
    assert upload["files_uploaded"] == 5  # type: ignore[index]
    assert upload["uploaded_bytes"] == 60  # type: ignore[index]
    assert "rate_bytes_per_second" not in upload


def test_upload_progress_does_not_regress_when_remote_status_lags() -> None:
    item = SubmissionInputFile(
        source=Path("clip-2.mp4"),
        rel_path="video/clip-2.mp4",
        bytes=10,
        sha256="0" * 64,
    )
    renderer_jobs: list[dict[str, object]] = []

    class Renderer:
        def update(self, job: dict[str, object], *, force: bool = False) -> None:
            renderer_jobs.append(job)

    progress = UploadProgress(
        total_files=3,
        total_bytes=30,
        completed_files=1,
        completed_bytes=10,
        renderer=Renderer(),  # type: ignore[arg-type]
        job_status_provider=lambda: {
            "upload_progress": {
                "files_uploaded": 1,
                "files_total": 3,
                "uploaded_bytes": 10,
                "bytes_total": 30,
            },
        },
    )
    progress.last_printed_at -= 20

    progress.mark_complete(item)

    upload = renderer_jobs[0]["upload_progress"]
    assert upload["files_uploaded"] == 2  # type: ignore[index]
    assert upload["uploaded_bytes"] == 20  # type: ignore[index]


def test_upload_progress_does_not_regress_from_previous_remote_sample() -> None:
    progress = UploadProgress(
        total_files=10,
        total_bytes=100,
        completed_files=2,
        completed_bytes=20,
    )

    first = progress.remote_upload_progress_with_rate(
        {
            "files_uploaded": 5,
            "files_total": 10,
            "uploaded_bytes": 60,
            "bytes_total": 100,
        },
        now=10.0,
    )
    second = progress.remote_upload_progress_with_rate(
        {
            "files_uploaded": 4,
            "files_total": 10,
            "uploaded_bytes": 40,
            "bytes_total": 100,
        },
        now=20.0,
    )
    third = progress.remote_upload_progress_with_rate(
        {
            "files_uploaded": 6,
            "files_total": 10,
            "uploaded_bytes": 75,
            "bytes_total": 100,
        },
        now=25.0,
    )

    assert first["files_uploaded"] == 5
    assert first["uploaded_bytes"] == 60
    assert second["files_uploaded"] == 5
    assert second["uploaded_bytes"] == 60
    assert "rate_bytes_per_second" not in second
    assert third["files_uploaded"] == 6
    assert third["uploaded_bytes"] == 75
    assert third["rate_bytes_per_second"] == 1


def test_upload_progress_does_not_regress_when_local_sample_lags_remote() -> None:
    item = SubmissionInputFile(
        source=Path("clip-3.mp4"),
        rel_path="video/clip-3.mp4",
        bytes=20,
        sha256="0" * 64,
    )
    renderer_jobs: list[dict[str, object]] = []

    class Renderer:
        def update(self, job: dict[str, object], *, force: bool = False) -> None:
            renderer_jobs.append(job)

    progress = UploadProgress(
        total_files=10,
        total_bytes=100,
        completed_files=1,
        completed_bytes=20,
        renderer=Renderer(),  # type: ignore[arg-type]
    )
    progress.remote_upload_progress_with_rate(
        {
            "files_uploaded": 5,
            "files_total": 10,
            "uploaded_bytes": 60,
            "bytes_total": 100,
        },
        now=10.0,
    )
    progress.last_printed_at -= 20

    progress.mark_complete(item)

    upload = renderer_jobs[0]["upload_progress"]
    assert upload["files_uploaded"] == 5  # type: ignore[index]
    assert upload["uploaded_bytes"] == 60  # type: ignore[index]


def test_upload_progress_fallback_uses_full_upload_baseline_on_resume() -> None:
    item = SubmissionInputFile(
        source=Path("pending.mp4"),
        rel_path="video/pending.mp4",
        bytes=4,
        sha256="0" * 64,
    )
    renderer_jobs: list[dict[str, object]] = []

    class Renderer:
        def update(self, job: dict[str, object], *, force: bool = False) -> None:
            renderer_jobs.append(job)

    progress = UploadProgress(
        total_files=3,
        total_bytes=12,
        completed_files=2,
        completed_bytes=8,
        renderer=Renderer(),  # type: ignore[arg-type]
    )

    progress.mark_complete(item)

    upload = renderer_jobs[0]["upload_progress"]
    assert upload["files_uploaded"] == 3  # type: ignore[index]
    assert upload["files_total"] == 3  # type: ignore[index]
    assert upload["uploaded_bytes"] == 12  # type: ignore[index]
    assert upload["bytes_total"] == 12  # type: ignore[index]


def test_upload_progress_stops_when_munchy_job_fails() -> None:
    item = SubmissionInputFile(
        source=Path("clip.mp4"),
        rel_path="video/clip.mp4",
        bytes=4,
        sha256="0" * 64,
    )
    stop_event = Event()
    renderer_jobs: list[dict[str, object]] = []

    class Renderer:
        def update(self, job: dict[str, object], *, force: bool = False) -> None:
            renderer_jobs.append(job)

    progress = UploadProgress(
        total_files=2,
        total_bytes=8,
        renderer=Renderer(),  # type: ignore[arg-type]
        stop_event=stop_event,
        job_status_provider=lambda: {
            "job_id": "job-1",
            "state": "failed",
            "phase": "eager_archive:pipeline=3/3",
            "error": "archive video encode failed",
        },
    )
    progress.last_printed_at -= 20

    with pytest.raises(JobTerminalDuringUpload) as exc_info:
        progress.mark_complete(item)

    assert stop_event.is_set()
    assert "archive video encode failed" in str(exc_info.value)
    assert renderer_jobs[-1]["state"] == "failed"

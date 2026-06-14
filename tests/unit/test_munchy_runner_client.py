from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from threading import Event
from unittest.mock import patch

import pytest

from munchy.runner_client import (
    CLEANUP_REQUEST_TIMEOUT_SECONDS,
    MunchyRunnerClient,
    RichProgressRenderer,
    RunnerHttpError,
    RunnerInputFile,
    RunnerJobTerminalDuringUpload,
    RunnerUploadRequest,
    UploadProgress,
    UploadRetryReporter,
    format_job_failure,
    format_job_status_line,
    format_job_summary_line,
    format_progress_status_line,
    format_riverhog_archive_progress,
    format_riverhog_promotion_progress,
    format_riverhog_upload_progress,
    job_finished_cleanly,
    progress_percent,
    riverhog_archive_progress,
    riverhog_promotion_progress,
)


def test_format_job_summary_line_includes_upload_and_encode_progress() -> None:
    line = format_job_summary_line(
        {
            "job_id": "job-1",
            "collection_slug": "example-q49",
            "state": "running",
            "phase": "gpu-eager:pipeline=3/3",
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

    assert line.startswith("job-1 [example-q49] | job: running | gpu-eager:pipeline=3/3")
    assert "remote upload 10/20 files" in line
    assert "remote encode 4/20 files" in line
    assert "batches 1/3" in line


def test_format_riverhog_upload_progress_uses_expected_file_total() -> None:
    line = format_riverhog_upload_progress(
        {
            "primary_files_uploaded": 3,
            "primary_files_total": 20,
            "primary_files_encoded": 4,
            "artifact_files_uploaded": 6,
            "artifact_files_known": 7,
            "artifact_files_registered": 6,
            "uploaded_bytes": 1_614_000,
            "bytes_total": 2_130_000,
            "percent_primary_files": 15.0,
            "rate_bytes_per_second": 9_000,
            "state": "open",
        }
    )

    assert line.startswith("riverhog handoff 3/20 recordings delivered")
    assert "15.00%" in line
    assert "4 encoded" in line
    assert "6/7 artifacts" in line
    assert "1.54 MiB uploaded" in line
    assert "72." not in line


def test_format_riverhog_archive_and_promotion_progress_are_separate() -> None:
    progress = {
        "collection_id": "2026/camera",
        "archive_phase": "uploading",
        "archive_uploaded_bytes": 4_000,
        "archive_total_bytes": 10_000,
        "archive_uploaded_parts": 2,
        "archive_total_parts": 5,
        "hot_promoted_files": 3,
        "riverhog_files_total": 10,
        "hot_promoted_bytes": 6_000,
        "riverhog_bytes_total": 20_000,
    }

    archive = riverhog_archive_progress(progress)
    promotion = riverhog_promotion_progress(progress)

    assert archive is not None
    assert promotion is not None
    assert format_riverhog_archive_progress(archive).startswith("riverhog archive, uploading")
    assert "parts 2/5" in format_riverhog_archive_progress(archive)
    assert format_riverhog_promotion_progress(promotion).startswith(
        "riverhog promotion 3/10 files"
    )


def test_format_job_summary_line_renders_review_clip_progress() -> None:
    line = format_job_summary_line(
        {
            "job_id": "job-review",
            "collection_slug": "camera-review-q49",
            "state": "running",
            "phase": "review_upload_retrying",
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


def test_format_job_summary_line_includes_encoder_queue_position() -> None:
    line = format_job_summary_line(
        {
            "job_id": "job-2",
            "collection_slug": "example-q49",
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
            "collection_slug": "camera-preview",
            "state": "failed",
            "phase": "collection_preview_upload",
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
    assert "- collection: camera-preview" in failure
    assert "- status: job: failed" in failure
    assert "- error: rclone failed after many retries" in failure
    assert "- gpu statuses.batch-1.error: ffmpeg failed for camera/clip.mp4" in failure
    assert "eager_archive" not in failure
    assert len(failure) < 700


def test_runner_client_list_jobs_validates_response() -> None:
    client = MunchyRunnerClient("http://runner")

    def fake_json(method: str, path: str, **_kwargs: object) -> dict[str, object]:
        assert method == "GET"
        assert path == "/v1/jobs?include_terminal=false&limit=2"
        return {"jobs": [{"job_id": "job-1"}, "ignored"]}

    client.json = fake_json  # type: ignore[method-assign]

    assert client.list_jobs(limit=2) == [{"job_id": "job-1"}]


def test_wait_for_job_polls_compact_status() -> None:
    client = MunchyRunnerClient("http://runner")
    calls: list[bool] = []

    def fake_get_job(job_id: str, *, compact: bool = False) -> dict[str, object]:
        calls.append(compact)
        return {"job_id": job_id, "state": "succeeded", "phase": "done"}

    client.get_job = fake_get_job  # type: ignore[method-assign]

    assert client.wait_for_job("job-1", interval=0)["state"] == "succeeded"
    assert calls == [True]


def test_wait_for_job_continues_until_riverhog_safe_to_delete() -> None:
    client = MunchyRunnerClient("http://runner")
    responses: list[dict[str, object]] = [
        {
            "job_id": "job-1",
            "state": "succeeded",
            "riverhog_upload_progress": {
                "collection_id": "2026/camera",
                "state": "archiving",
                "safe_to_delete": False,
            },
        },
        {
            "job_id": "job-1",
            "state": "succeeded",
            "riverhog_upload_progress": {
                "collection_id": "2026/camera",
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


def test_job_finished_cleanly_rejects_unfinalized_riverhog_job() -> None:
    assert not job_finished_cleanly(
        {
            "state": "succeeded",
            "riverhog_upload_progress": {
                "collection_id": "2026/camera",
                "state": "archiving",
                "safe_to_delete": False,
            },
        }
    )


def test_cancel_job_cleanup_uses_long_timeout() -> None:
    client = MunchyRunnerClient("http://runner")
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
        return {"job_id": "job-1", "state": "cancelled"}

    client.json = fake_json  # type: ignore[method-assign]

    client.cancel_job("job-1", cleanup=True)

    assert seen == {
        "method": "POST",
        "path": "/v1/jobs/job-1/cancel?cleanup=true",
        "timeout": CLEANUP_REQUEST_TIMEOUT_SECONDS,
        "expect": {202},
    }


def test_runner_http_error_formats_insufficient_storage_concisely() -> None:
    error = RunnerHttpError(
        "POST",
        "http://runner/v1/input-uploads",
        507,
        b"""
        {
          "detail": {
            "error": "insufficient_storage",
            "label": "source upload spool, future gpu scratch",
            "required_bytes": 2147483648,
            "free_bytes": 1073741824,
            "reserved_bytes": 536870912
          }
        }
        """,
    )

    assert "insufficient storage for source upload spool, future gpu scratch" in str(error)
    assert "need 2.00 GiB free" in str(error)
    assert "have 1.00 GiB" in str(error)
    assert "512.00 MiB reserved by active uploads" in str(error)


def test_create_input_upload_sends_filesystem_metadata(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    metadata = {
        "kind": "munchy.source-filesystem-metadata",
        "stat": {"st_birthtime": 1.25},
    }
    item = RunnerInputFile(
        source=source,
        rel_path="video/clip.mp4",
        bytes=source.stat().st_size,
        sha256="0" * 64,
        filesystem_metadata=metadata,
    )
    request = RunnerUploadRequest(
        upload_id="upload-1",
        job_id="job-1",
        files=(item,),
        storage_hint={"source_bytes": item.bytes},
        job_payload={"job_id": "job-1", "input_upload_id": "upload-1"},
    )
    client = MunchyRunnerClient("http://runner")
    seen_payload: dict[str, object] = {}

    def fake_request(
        method: str,
        path: str,
        **kwargs: object,
    ) -> tuple[int, bytes, object]:
        assert method == "POST"
        assert path == "/v1/input-uploads"
        seen_payload.update(kwargs["payload"])  # type: ignore[arg-type]
        return (
            201,
            json.dumps({"upload_id": "upload-1", "files": seen_payload["files"]}).encode(),
            object(),
        )

    client.request = fake_request  # type: ignore[method-assign]

    upload = client.create_or_get_input_upload(request)

    assert upload["upload_id"] == "upload-1"
    assert seen_payload["files"] == [
        {
            "path": "video/clip.mp4",
            "bytes": item.bytes,
            "sha256": "0" * 64,
            "filesystem_metadata": metadata,
        }
    ]


def test_upload_file_retries_transient_error_and_refreshes_offset(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    item = RunnerInputFile(
        source=source,
        rel_path="video/clip.mp4",
        bytes=source.stat().st_size,
        sha256="0" * 64,
    )

    client = MunchyRunnerClient("http://runner")
    json_calls = 0
    request_calls = 0

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

    def fake_request(
        method: str,
        path_or_url: str,
        **_kwargs: object,
    ) -> tuple[int, bytes, object]:
        nonlocal request_calls
        request_calls += 1
        raise ConnectionResetError(54, "Connection reset by peer")

    client.json = fake_json  # type: ignore[method-assign]
    client.request = fake_request  # type: ignore[method-assign]

    with patch("munchy.runner_client.time.sleep"):
        with contextlib.redirect_stderr(io.StringIO()):
            client.upload_file("upload", item, chunk_bytes=1024)

    assert json_calls == 2
    assert request_calls == 1


def test_upload_file_retries_temporary_insufficient_storage(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    item = RunnerInputFile(
        source=source,
        rel_path="video/clip.mp4",
        bytes=source.stat().st_size,
        sha256="0" * 64,
    )

    client = MunchyRunnerClient("http://runner")
    json_calls = 0
    request_calls = 0

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

    def fake_request(
        method: str,
        path_or_url: str,
        **_kwargs: object,
    ) -> tuple[int, bytes, object]:
        nonlocal request_calls
        request_calls += 1
        raise RunnerHttpError(method, path_or_url, 507, b"temporary storage pressure")

    client.json = fake_json  # type: ignore[method-assign]
    client.request = fake_request  # type: ignore[method-assign]

    with patch("munchy.runner_client.time.sleep"):
        with contextlib.redirect_stderr(io.StringIO()):
            client.upload_file("upload", item, chunk_bytes=1024)

    assert json_calls == 2
    assert request_calls == 1


def test_upload_file_does_not_retry_non_transient_http_error(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    item = RunnerInputFile(
        source=source,
        rel_path="video/clip.mp4",
        bytes=source.stat().st_size,
        sha256="0" * 64,
    )
    client = MunchyRunnerClient("http://runner")

    def fake_json(method: str, path: str, **_kwargs: object) -> dict[str, object]:
        return {
            "upload_url": "http://uploads.test/file",
            "offset": 0,
            "length": item.bytes,
        }

    def fake_request(
        method: str,
        path_or_url: str,
        **_kwargs: object,
    ) -> tuple[int, bytes, object]:
        raise RunnerHttpError(method, path_or_url, 400, b"bad request")

    client.json = fake_json  # type: ignore[method-assign]
    client.request = fake_request  # type: ignore[method-assign]

    with pytest.raises(RunnerHttpError):
        client.upload_file("upload", item, chunk_bytes=1024)


def test_upload_files_skips_completed_paths(tmp_path: Path) -> None:
    files = []
    for index in range(3):
        source = tmp_path / f"clip{index}.mp4"
        source.write_bytes(b"video")
        files.append(
            RunnerInputFile(
                source=source,
                rel_path=f"video/clip{index}.mp4",
                bytes=source.stat().st_size,
                sha256=str(index) * 64,
            )
        )
    request = RunnerUploadRequest(
        upload_id="upload-1",
        job_id="job-1",
        files=tuple(files),
        storage_hint={"source_bytes": sum(item.bytes for item in files)},
        job_payload={"job_id": "job-1", "input_upload_id": "upload-1"},
        upload_workers=3,
        upload_chunk_mib=9,
    )
    client = MunchyRunnerClient("http://runner")
    uploaded: list[str] = []
    input_upload_gets = 0

    def fake_upload_file(
        upload_id: str,
        item: RunnerInputFile,
        *,
        chunk_bytes: int,
        retry_reporter: object | None = None,
        stop_event: object | None = None,
    ) -> None:
        uploaded.append(item.rel_path)

    def fake_json(method: str, path: str, **_kwargs: object) -> dict[str, object]:
        nonlocal input_upload_gets
        if path.startswith("/v1/jobs/"):
            return {}
        if path.startswith("/v1/input-uploads/"):
            input_upload_gets += 1
            return {
                "state": "uploaded" if input_upload_gets > 1 else "uploading",
                "files": [
                    {"path": files[0].rel_path, "complete": True},
                    {"path": files[1].rel_path, "complete": True},
                    {"path": files[2].rel_path, "complete": False},
                ],
            }
        raise AssertionError(path)

    client.upload_file = fake_upload_file  # type: ignore[method-assign]
    client.json = fake_json  # type: ignore[method-assign]

    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        client.upload_files(request)

    assert uploaded == [files[2].rel_path]
    assert "skipped 2 already complete files" in stderr.getvalue()


def test_upload_progress_can_merge_remote_upload_and_encode_progress() -> None:
    item = RunnerInputFile(
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


def test_upload_progress_does_not_regress_when_remote_status_lags() -> None:
    item = RunnerInputFile(
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


def test_upload_progress_fallback_uses_full_upload_baseline_on_resume() -> None:
    item = RunnerInputFile(
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


def test_upload_progress_stops_when_runner_job_fails() -> None:
    item = RunnerInputFile(
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
            "phase": "gpu-eager:pipeline=3/3",
            "error": "archive video encode failed",
        },
    )
    progress.last_printed_at -= 20

    with pytest.raises(RunnerJobTerminalDuringUpload) as exc_info:
        progress.mark_complete(item)

    assert stop_event.is_set()
    assert "archive video encode failed" in str(exc_info.value)
    assert renderer_jobs[-1]["state"] == "failed"

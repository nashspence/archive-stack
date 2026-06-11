from __future__ import annotations

import contextlib
import io
from pathlib import Path
from unittest.mock import patch

import pytest

from munchy.runner_client import (
    MunchyRunnerClient,
    RichProgressRenderer,
    RunnerHttpError,
    RunnerInputFile,
    RunnerUploadRequest,
    UploadRetryReporter,
    UploadProgress,
    format_progress_status_line,
    format_job_summary_line,
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
    assert "input tree 5/10 files" in line


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
    assert any("Remote Transient Issue" in line for line in without_issue)
    assert any("Remote Transient Issue" in line for line in with_issue)
    assert all(len(line) <= 72 for line in with_issue)


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


def test_runner_client_list_jobs_validates_response() -> None:
    client = MunchyRunnerClient("http://runner")

    def fake_json(method: str, path: str, **_kwargs: object) -> dict[str, object]:
        assert method == "GET"
        assert path == "/v1/jobs?include_terminal=false&limit=2"
        return {"jobs": [{"job_id": "job-1"}, "ignored"]}

    client.json = fake_json  # type: ignore[method-assign]

    assert client.list_jobs(limit=2) == [{"job_id": "job-1"}]


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

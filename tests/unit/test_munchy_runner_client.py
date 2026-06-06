from __future__ import annotations

import contextlib
import io
from pathlib import Path
from unittest.mock import patch

import pytest

from munchy.runner_client import (
    MunchyRunnerClient,
    RunnerHttpError,
    RunnerInputFile,
    RunnerUploadRequest,
    UploadProgress,
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
    assert "upload 10/20 files" in line
    assert "encode 4/20 files" in line
    assert "batches 1/3" in line


def test_runner_client_list_jobs_validates_response() -> None:
    client = MunchyRunnerClient("http://runner")

    def fake_json(method: str, path: str, **_kwargs: object) -> dict[str, object]:
        assert method == "GET"
        assert path == "/v1/jobs?include_terminal=false&limit=2"
        return {"jobs": [{"job_id": "job-1"}, "ignored"]}

    client.json = fake_json  # type: ignore[method-assign]

    assert client.list_jobs(limit=2) == [{"job_id": "job-1"}]


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

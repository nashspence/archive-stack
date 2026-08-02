from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from media_preflight import (
    PREFLIGHT_CACHE_SCHEMA_VERSION,
    MediaPreflightCache,
    MediaPreflightCacheFile,
    MediaPreflightFile,
    MediaPreflightIssue,
    MediaPreflightReport,
    MediaPreflightResult,
    run_cached_media_preflight,
    run_media_preflight,
)


def mp4_atom(atom_type: bytes, payload: bytes) -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + atom_type + payload


def test_media_preflight_detects_truncated_mp4_atom(tmp_path: Path) -> None:
    source = tmp_path / "bad.mp4"
    source.write_bytes(
        mp4_atom(b"ftyp", b"isom") + mp4_atom(b"moov", b"") + (20).to_bytes(4, "big") + b"mdat"
    )

    report = run_media_preflight(
        [
            MediaPreflightFile(
                source=source,
                label="camera/bad.mp4",
                bytes=source.stat().st_size,
            )
        ],
        ffprobe_path=None,
        progress=False,
    )

    assert not report.ok
    assert report.failed_results[0].issues[0].code == "mp4_atom_extends_past_eof"


def test_media_preflight_accepts_mp4_with_video_stream_metadata(tmp_path: Path) -> None:
    source = tmp_path / "ok.mp4"
    source.write_bytes(
        mp4_atom(b"ftyp", b"isom") + mp4_atom(b"moov", b"") + mp4_atom(b"mdat", b"payload")
    )

    def fake_run(command, *, check, capture_output, text, timeout):
        assert command[0] == "ffprobe"
        assert check is False
        assert capture_output is True
        assert text is True
        assert timeout > 0
        return subprocess.CompletedProcess(
            command,
            0,
            (
                '{"streams":[{"index":0,"codec_type":"video","codec_name":"h264",'
                '"width":1920,"height":1080,"pix_fmt":"yuv420p","duration":"1.5"}]}'
            ),
            "",
        )

    with patch("media_preflight.subprocess.run", fake_run):
        report = run_media_preflight(
            [
                MediaPreflightFile(
                    source=source,
                    label="camera/ok.mp4",
                    bytes=source.stat().st_size,
                )
            ],
            progress=False,
        )

    assert report.ok


def test_media_preflight_rejects_video_stream_without_usable_metadata(tmp_path: Path) -> None:
    source = tmp_path / "bad.mp4"
    source.write_bytes(
        mp4_atom(b"ftyp", b"isom") + mp4_atom(b"moov", b"") + mp4_atom(b"mdat", b"payload")
    )

    def fake_run(command, *, check, capture_output, text, timeout):
        return subprocess.CompletedProcess(
            command,
            0,
            (
                '{"streams":[{"index":0,"codec_type":"video","codec_name":"h264",'
                '"width":2880,"height":1616,"pix_fmt":"none"}],"format":{}}'
            ),
            "",
        )

    with patch("media_preflight.subprocess.run", fake_run):
        report = run_media_preflight(
            [
                MediaPreflightFile(
                    source=source,
                    label="camera/bad.mp4",
                    bytes=source.stat().st_size,
                )
            ],
            progress=False,
        )

    assert not report.ok
    assert report.failed_results[0].issues[0].code == "ffprobe_no_usable_video_stream"


def test_cached_media_preflight_reports_local_progress(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    updates: list[dict[str, object]] = []

    class Renderer:
        def update(self, job: dict[str, object], *, force: bool = False) -> None:
            updates.append(job)

    file = MediaPreflightCacheFile(
        source=source,
        label="camera/clip.mp4",
        bytes=source.stat().st_size,
        sha256="a" * 64,
    )

    def fake_run_media_preflight(
        files: list[MediaPreflightFile],
        *,
        progress: bool = True,
        progress_callback: object | None = None,
    ) -> MediaPreflightReport:
        if callable(progress_callback):
            progress_callback(
                {
                    "files_done": 1,
                    "bytes_done": files[0].bytes,
                    "failures": 0,
                }
            )
        return MediaPreflightReport(
            [MediaPreflightResult(file=files[0], issues=[])],
            elapsed_seconds=0.1,
        )

    with patch("media_preflight.run_media_preflight", fake_run_media_preflight):
        report, _stats = run_cached_media_preflight(
            [file],
            cache=None,
            renderer=Renderer(),
        )

    assert report.ok
    progress = updates[-1]["local_progress"]["preflight"]  # type: ignore[index]
    assert progress["files_done"] == 1
    assert progress["files_total"] == 1
    assert progress["bytes_done"] == source.stat().st_size
    assert progress["completed"] is True


def test_media_preflight_cache_commits_each_write_for_parallel_runs(tmp_path: Path) -> None:
    preflight_path = tmp_path / "preflight.sqlite3"
    file = MediaPreflightCacheFile(
        source=tmp_path / "a.mp4",
        label="camera/a.mp4",
        bytes=100,
        sha256="a" * 64,
    )
    with (
        MediaPreflightCache(preflight_path) as first,
        MediaPreflightCache(preflight_path) as second,
    ):
        first.put(file, [MediaPreflightIssue("ffprobe_failed", "broken")])
        assert first.conn is not None
        assert not first.conn.in_transaction
        issues = second.get(file)
        assert issues == [MediaPreflightIssue("ffprobe_failed", "broken")]
        assert first.conn.execute("PRAGMA user_version").fetchone()[0] == (
            PREFLIGHT_CACHE_SCHEMA_VERSION
        )

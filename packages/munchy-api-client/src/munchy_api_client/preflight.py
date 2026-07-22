from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from time_formats import utc_timestamp_now

MP4_LIKE_EXTENSIONS = {".3g2", ".3gp", ".m4v", ".mov", ".mp4"}
FFPROBE_TIMEOUT_ENV = "MUNCHY_PREFLIGHT_FFPROBE_TIMEOUT"
DEFAULT_FFPROBE_TIMEOUT_SECONDS = 30.0
PREFLIGHT_CACHE_VERSION = 2
SQLITE_CACHE_TIMEOUT_SECONDS = 60.0
SQLITE_CACHE_BUSY_TIMEOUT_MS = int(SQLITE_CACHE_TIMEOUT_SECONDS * 1000)


class ProgressRenderer(Protocol):
    def update(self, update: dict[str, Any], *, force: bool = False) -> None: ...


@dataclass(frozen=True)
class MediaPreflightFile:
    source: Path
    label: str
    bytes: int


@dataclass(frozen=True)
class MediaPreflightCacheFile(MediaPreflightFile):
    sha256: str


@dataclass(frozen=True)
class MediaPreflightIssue:
    code: str
    message: str


@dataclass(frozen=True)
class MediaPreflightResult:
    file: MediaPreflightFile
    issues: list[MediaPreflightIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


@dataclass(frozen=True)
class MediaPreflightReport:
    results: list[MediaPreflightResult]
    elapsed_seconds: float

    @property
    def ok(self) -> bool:
        return all(result.ok for result in self.results)

    @property
    def failed_results(self) -> list[MediaPreflightResult]:
        return [result for result in self.results if not result.ok]

    @property
    def total_bytes(self) -> int:
        return sum(result.file.bytes for result in self.results)


@dataclass
class PreflightCacheStats:
    path: str | None = None
    hits: int = 0
    misses: int = 0
    writes: int = 0
    seeded_from_existing_upload: int = 0


class MediaPreflightError(RuntimeError):
    def __init__(self, report: MediaPreflightReport) -> None:
        failed = len(report.failed_results)
        total = len(report.results)
        super().__init__(f"media preflight failed for {failed}/{total} file(s); no upload started")
        self.report = report


class MediaPreflightCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.stats = PreflightCacheStats(path=str(path))
        self.conn: sqlite3.Connection | None = None

    def __enter__(self) -> MediaPreflightCache:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=SQLITE_CACHE_TIMEOUT_SECONDS)
        self.conn.execute(f"PRAGMA busy_timeout={SQLITE_CACHE_BUSY_TIMEOUT_MS}")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS media_preflight (
                sha256 TEXT NOT NULL,
                size INTEGER NOT NULL,
                version INTEGER NOT NULL,
                issues_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (sha256, size, version)
            )
            """
        )
        self.conn.commit()
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.conn is not None:
            self.conn.commit()
            self.conn.close()
            self.conn = None

    def get(self, file: MediaPreflightCacheFile) -> list[MediaPreflightIssue] | None:
        if self.conn is None:
            return None
        try:
            row = self.conn.execute(
                """
                SELECT issues_json FROM media_preflight
                WHERE sha256 = ? AND size = ? AND version = ?
                """,
                (file.sha256, file.bytes, PREFLIGHT_CACHE_VERSION),
            ).fetchone()
        except sqlite3.OperationalError:
            self.stats.misses += 1
            return None
        if row is None:
            self.stats.misses += 1
            return None
        self.stats.hits += 1
        issues = json.loads(str(row[0]))
        if not isinstance(issues, list):
            return []
        return [
            MediaPreflightIssue(str(item.get("code") or ""), str(item.get("message") or ""))
            for item in issues
            if isinstance(item, dict)
        ]

    def put(self, file: MediaPreflightCacheFile, issues: list[MediaPreflightIssue]) -> None:
        if self.conn is None:
            return
        payload = json.dumps(
            [{"code": issue.code, "message": issue.message} for issue in issues],
            sort_keys=True,
        )
        try:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO media_preflight
                    (sha256, size, version, issues_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    file.sha256,
                    file.bytes,
                    PREFLIGHT_CACHE_VERSION,
                    payload,
                    utc_timestamp_now(),
                ),
            )
            self.conn.commit()
        except sqlite3.OperationalError:
            self.conn.rollback()
            return
        self.stats.writes += 1

    def put_ok_from_existing_upload(self, files: list[MediaPreflightCacheFile]) -> None:
        for file in files:
            self.put(file, [])
            self.stats.seeded_from_existing_upload += 1


def ffprobe_timeout_seconds() -> float:
    configured = os.getenv(FFPROBE_TIMEOUT_ENV)
    if not configured:
        return DEFAULT_FFPROBE_TIMEOUT_SECONDS
    try:
        value = float(configured)
    except ValueError as exc:
        raise ValueError(f"{FFPROBE_TIMEOUT_ENV} must be a number") from exc
    if value <= 0:
        raise ValueError(f"{FFPROBE_TIMEOUT_ENV} must be positive")
    return value


def run_media_preflight(
    files: list[MediaPreflightFile],
    *,
    ffprobe_path: str | None = "ffprobe",
    progress: bool = True,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> MediaPreflightReport:
    started_at = time.monotonic()
    last_printed_at = started_at
    results: list[MediaPreflightResult] = []
    total_files = len(files)
    total_bytes = sum(item.bytes for item in files)
    timeout = ffprobe_timeout_seconds() if ffprobe_path else None

    for index, item in enumerate(files, start=1):
        issues: list[MediaPreflightIssue] = []
        if item.source.suffix.lower() in MP4_LIKE_EXTENSIONS:
            issues.extend(check_mp4_top_level_atoms(item.source))
        if ffprobe_path:
            issues.extend(check_ffprobe_video_stream(item.source, ffprobe_path, timeout or 0))
        results.append(MediaPreflightResult(file=item, issues=issues))

        now = time.monotonic()
        is_final = index == total_files
        if (progress or progress_callback is not None) and (
            is_final or now - last_printed_at >= 15
        ):
            elapsed = max(now - started_at, 0.001)
            checked_bytes = sum(result.file.bytes for result in results)
            pct = (checked_bytes / total_bytes * 100.0) if total_bytes else 100.0
            payload = {
                "stage": "preflight",
                "label": "preflight",
                "files_done": index,
                "files_total": total_files,
                "bytes_done": checked_bytes,
                "bytes_total": total_bytes,
                "percent_bytes": round(pct, 2),
                "elapsed_seconds": round(elapsed, 3),
                "failures": sum(1 for result in results if not result.ok),
                "completed": is_final,
            }
            if progress_callback is not None:
                progress_callback(payload)
            elif progress:
                print(
                    (
                        "preflight progress: "
                        f"{index}/{total_files} files, "
                        f"{checked_bytes / 1024**3:.2f}/{total_bytes / 1024**3:.2f} GiB, "
                        f"{pct:.2f}%, {elapsed:.1f}s"
                    ),
                    file=sys.stderr,
                )
            last_printed_at = now

    return MediaPreflightReport(
        results=results,
        elapsed_seconds=time.monotonic() - started_at,
    )


def media_preflight_report_from_results(
    results: list[MediaPreflightResult],
    *,
    started_at: float,
) -> MediaPreflightReport:
    return MediaPreflightReport(
        results=results,
        elapsed_seconds=time.monotonic() - started_at,
    )


def emit_local_preflight_progress(
    renderer: ProgressRenderer | None,
    *,
    files_done: int,
    files_total: int,
    bytes_done: int,
    bytes_total: int,
    started_at: float,
    stats: PreflightCacheStats,
    failures: int = 0,
    completed: bool = False,
) -> None:
    if renderer is None:
        return
    elapsed = max(time.monotonic() - started_at, 0.001)
    pct = (bytes_done / bytes_total * 100.0) if bytes_total else 100.0
    renderer.update(
        {
            "local_progress": {
                "preflight": {
                    "stage": "preflight",
                    "label": "preflight",
                    "files_done": files_done,
                    "files_total": files_total,
                    "bytes_done": bytes_done,
                    "bytes_total": bytes_total,
                    "percent_bytes": round(pct, 2),
                    "elapsed_seconds": round(elapsed, 3),
                    "cache_hits": stats.hits,
                    "cache_misses": stats.misses,
                    "cache_writes": stats.writes,
                    "cache_seeded": stats.seeded_from_existing_upload,
                    "failures": failures,
                    "completed": completed,
                }
            }
        },
        force=completed,
    )


def run_cached_media_preflight(
    files: list[MediaPreflightCacheFile],
    *,
    cache: MediaPreflightCache | None,
    cache_enabled: bool = True,
    existing_upload_matches_files: Callable[[list[MediaPreflightCacheFile]], bool] | None = None,
    renderer: ProgressRenderer | None = None,
) -> tuple[MediaPreflightReport, PreflightCacheStats]:
    started_at = time.monotonic()
    stats = cache.stats if cache is not None else PreflightCacheStats()
    if cache is None and not cache_enabled:
        stats.path = None
    total_files = len(files)
    total_bytes = sum(file.bytes for file in files)
    done_files = 0
    done_bytes = 0
    failures = 0

    results_by_label: dict[str, MediaPreflightResult] = {}
    missing: list[MediaPreflightCacheFile] = []
    for file in files:
        cached_issues = cache.get(file) if cache is not None else None
        if cached_issues is None:
            missing.append(file)
            continue
        done_files += 1
        done_bytes += file.bytes
        if cached_issues:
            failures += 1
        results_by_label[file.label] = MediaPreflightResult(
            file=MediaPreflightFile(source=file.source, label=file.label, bytes=file.bytes),
            issues=cached_issues,
        )
    emit_local_preflight_progress(
        renderer,
        files_done=done_files,
        files_total=total_files,
        bytes_done=done_bytes,
        bytes_total=total_bytes,
        started_at=started_at,
        stats=stats,
        failures=failures,
        completed=not missing,
    )

    if (
        missing
        and cache is not None
        and existing_upload_matches_files is not None
        and existing_upload_matches_files(files)
    ):
        cache.put_ok_from_existing_upload(missing)
        for file in missing:
            results_by_label[file.label] = MediaPreflightResult(
                file=MediaPreflightFile(source=file.source, label=file.label, bytes=file.bytes),
                issues=[],
            )
            done_files += 1
            done_bytes += file.bytes
        missing = []
        emit_local_preflight_progress(
            renderer,
            files_done=done_files,
            files_total=total_files,
            bytes_done=done_bytes,
            bytes_total=total_bytes,
            started_at=started_at,
            stats=stats,
            failures=failures,
            completed=True,
        )

    if missing:
        base_done_files = done_files
        base_done_bytes = done_bytes
        base_failures = failures

        def preflight_progress(payload: dict[str, Any]) -> None:
            emit_local_preflight_progress(
                renderer,
                files_done=base_done_files + int(payload.get("files_done") or 0),
                files_total=total_files,
                bytes_done=base_done_bytes + int(payload.get("bytes_done") or 0),
                bytes_total=total_bytes,
                started_at=started_at,
                stats=stats,
                failures=base_failures + int(payload.get("failures") or 0),
                completed=False,
            )

        checked_report = run_media_preflight(
            [
                MediaPreflightFile(source=file.source, label=file.label, bytes=file.bytes)
                for file in missing
            ],
            progress=renderer is None,
            progress_callback=preflight_progress if renderer is not None else None,
        )
        missing_by_label = {result.file.label: result for result in checked_report.results}
        for file in missing:
            result = missing_by_label[file.label]
            if cache is not None:
                cache.put(file, result.issues)
            done_files += 1
            done_bytes += file.bytes
            if result.issues:
                failures += 1
            results_by_label[file.label] = result

    results = [results_by_label[file.label] for file in files]
    emit_local_preflight_progress(
        renderer,
        files_done=total_files,
        files_total=total_files,
        bytes_done=total_bytes,
        bytes_total=total_bytes,
        started_at=started_at,
        stats=stats,
        failures=sum(1 for result in results if not result.ok),
        completed=True,
    )
    return media_preflight_report_from_results(results, started_at=started_at), stats


def check_mp4_top_level_atoms(path: Path) -> list[MediaPreflightIssue]:
    issues: list[MediaPreflightIssue] = []
    file_size = path.stat().st_size
    offset = 0
    atom_types: list[bytes] = []
    with path.open("rb") as handle:
        while offset < file_size:
            remaining = file_size - offset
            if remaining < 8:
                issues.append(
                    MediaPreflightIssue(
                        "mp4_partial_atom_header",
                        f"partial top-level atom header at byte {offset}; file size is {file_size}",
                    )
                )
                break
            handle.seek(offset)
            header = handle.read(8)
            if len(header) != 8:
                issues.append(
                    MediaPreflightIssue(
                        "mp4_partial_atom_header",
                        f"could not read full top-level atom header at byte {offset}",
                    )
                )
                break
            atom_size = int.from_bytes(header[:4], "big")
            atom_type = header[4:8]
            atom_types.append(atom_type)
            header_size = 8
            if atom_size == 1:
                extended = handle.read(8)
                if len(extended) != 8:
                    issues.append(
                        MediaPreflightIssue(
                            "mp4_partial_extended_size",
                            (
                                "could not read extended size for top-level atom "
                                f"{atom_name(atom_type)} at byte {offset}"
                            ),
                        )
                    )
                    break
                atom_size = int.from_bytes(extended, "big")
                header_size = 16
            elif atom_size == 0:
                break

            if atom_size < header_size:
                issues.append(
                    MediaPreflightIssue(
                        "mp4_invalid_atom_size",
                        (
                            f"top-level atom {atom_name(atom_type)} at byte {offset} declares "
                            f"invalid size {atom_size}"
                        ),
                    )
                )
                break
            atom_end = offset + atom_size
            if atom_end > file_size:
                issues.append(
                    MediaPreflightIssue(
                        "mp4_atom_extends_past_eof",
                        (
                            f"top-level atom {atom_name(atom_type)} at byte {offset} declares "
                            f"size {atom_size}, ending at byte {atom_end}, "
                            f"but file size is {file_size}"
                        ),
                    )
                )
                break
            offset = atom_end

    if atom_types and b"moov" not in atom_types:
        issues.append(MediaPreflightIssue("mp4_missing_moov", "no top-level moov atom found"))
    if atom_types and b"mdat" not in atom_types:
        issues.append(MediaPreflightIssue("mp4_missing_mdat", "no top-level mdat atom found"))
    return issues


def atom_name(atom_type: bytes) -> str:
    return repr(atom_type.decode("ascii", "replace"))


def check_ffprobe_video_stream(
    path: Path,
    ffprobe_path: str,
    timeout_seconds: float,
) -> list[MediaPreflightIssue]:
    command = [
        ffprobe_path,
        "-v",
        "error",
        "-show_entries",
        "stream=index,codec_type,codec_name,width,height,pix_fmt,duration:format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError:
        return [MediaPreflightIssue("ffprobe_not_found", f"{ffprobe_path} was not found")]
    except subprocess.TimeoutExpired:
        return [
            MediaPreflightIssue(
                "ffprobe_timeout",
                f"ffprobe timed out after {timeout_seconds:g}s",
            )
        ]
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip() or "no ffprobe details"
        return [MediaPreflightIssue("ffprobe_failed", f"ffprobe failed: {stderr}")]
    try:
        parsed = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        return [
            MediaPreflightIssue(
                "ffprobe_invalid_json",
                f"ffprobe returned invalid JSON: {exc}",
            )
        ]
    streams = parsed.get("streams")
    if not isinstance(streams, list):
        return [MediaPreflightIssue("ffprobe_missing_streams", "ffprobe returned no stream list")]
    video_streams = [
        stream
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == "video"
    ]
    if not video_streams:
        return [MediaPreflightIssue("ffprobe_no_video_stream", "ffprobe found no video stream")]
    format_info = parsed.get("format")
    format_duration = (
        positive_float(format_info.get("duration")) if isinstance(format_info, dict) else None
    )
    if not any(usable_ffprobe_video_stream(stream, format_duration) for stream in video_streams):
        stream_summaries = ", ".join(
            (
                f"stream {stream.get('index', '?')} "
                f"{stream.get('codec_name') or 'unknown'} "
                f"{stream.get('width') or '?'}x{stream.get('height') or '?'} "
                f"pix_fmt={stream.get('pix_fmt') or 'missing'} "
                f"duration={stream.get('duration') or format_duration or 'missing'}"
            )
            for stream in video_streams[:3]
        )
        return [
            MediaPreflightIssue(
                "ffprobe_no_usable_video_stream",
                f"ffprobe found no usable video stream metadata ({stream_summaries})",
            )
        ]
    return []


def positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def usable_ffprobe_video_stream(stream: dict[str, Any], format_duration: float | None) -> bool:
    width = positive_int(stream.get("width"))
    height = positive_int(stream.get("height"))
    pix_fmt = str(stream.get("pix_fmt") or "").strip().lower()
    duration = positive_float(stream.get("duration")) or format_duration
    return bool(width and height and pix_fmt and pix_fmt != "none" and duration)


def print_media_preflight_report(report: MediaPreflightReport) -> None:
    if report.ok:
        print(
            (
                f"preflight ok: {len(report.results)} files, "
                f"{report.total_bytes / 1024**3:.2f} GiB checked in "
                f"{report.elapsed_seconds:.1f}s"
            ),
            file=sys.stderr,
        )
        return

    print(
        (
            f"preflight failed: {len(report.failed_results)}/{len(report.results)} files, "
            f"{report.total_bytes / 1024**3:.2f} GiB checked in "
            f"{report.elapsed_seconds:.1f}s"
        ),
        file=sys.stderr,
    )
    for result in report.failed_results:
        print(f"- {result.file.label} ({result.file.source})", file=sys.stderr)
        for issue in result.issues:
            print(f"  - {issue.code}: {issue.message}", file=sys.stderr)


def print_preflight_cache_stats(stats: PreflightCacheStats, total_files: int) -> None:
    if not stats.path:
        return
    checked = max(0, total_files - stats.hits - stats.seeded_from_existing_upload)
    print(
        (
            "preflight cache: "
            f"{stats.hits} hits, {checked} checked, "
            f"{stats.writes} writes"
            + (
                f", {stats.seeded_from_existing_upload} seeded from an existing upload"
                if stats.seeded_from_existing_upload
                else ""
            )
            + f" ({stats.path})"
        ),
        file=sys.stderr,
    )


def assert_media_preflight_ok(report: MediaPreflightReport) -> None:
    if not report.ok:
        raise MediaPreflightError(report)

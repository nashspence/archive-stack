from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MP4_LIKE_EXTENSIONS = {".3g2", ".3gp", ".m4v", ".mov", ".mp4"}
DEFAULT_FFPROBE_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class MediaPreflightFile:
    source: Path
    label: str
    bytes: int


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


def run_media_preflight(
    files: list[MediaPreflightFile],
    *,
    ffprobe_path: str | None = "ffprobe",
    ffprobe_timeout_seconds: float = DEFAULT_FFPROBE_TIMEOUT_SECONDS,
    progress: bool = True,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> MediaPreflightReport:
    started_at = time.monotonic()
    last_printed_at = started_at
    results: list[MediaPreflightResult] = []
    total_files = len(files)
    total_bytes = sum(item.bytes for item in files)
    if ffprobe_timeout_seconds <= 0:
        raise ValueError("ffprobe timeout must be positive")
    timeout = ffprobe_timeout_seconds if ffprobe_path else None

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

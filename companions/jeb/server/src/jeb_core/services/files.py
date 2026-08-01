from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import httpx
from media_preflight import (
    MP4_LIKE_EXTENSIONS,
    MediaPreflightReport,
)

from jeb_core.domain.models import (
    SOURCE_REMOVAL_CHALLENGE,
    JebIngressConfig,
    PreflightJebError,
    UnrecoverableJebError,
    stable_json,
)
from jeb_core.domain.sources import SourceRegistryError


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_posix(path: str | PurePosixPath) -> str:
    rel = PurePosixPath(str(path))
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        raise ValueError(f"path is not normalized relative POSIX: {path}")
    return rel.as_posix()


def same_file_inode(left: Path, right: Path) -> bool:
    left_stat = left.stat()
    right_stat = right.stat()
    return left_stat.st_dev == right_stat.st_dev and left_stat.st_ino == right_stat.st_ino


def hardlink_stage_file(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(str(dest).encode()).hexdigest()[:12]
    part = dest.with_name(f".{dest.name}.{digest}.part")
    if dest.exists():
        if dest.is_dir():
            raise UnrecoverableJebError(f"staging path is a directory: {dest}")
        if same_file_inode(source, dest):
            return
        dest.unlink()
    try:
        os.link(source, part)
        part.replace(dest)
    except OSError as exc:
        raise UnrecoverableJebError(
            "could not hardlink source into Jeb batch; keep JEB_BATCH_DIR "
            f"on the same filesystem as source landing directories: {source} -> {dest}"
        ) from exc
    finally:
        part.unlink(missing_ok=True)


def run_safe_remux(*, ffmpeg_path: str, source: Path, dest: Path) -> None:
    command = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-i",
        str(source),
        "-map",
        "0",
        "-c",
        "copy",
        "-copy_unknown",
    ]
    if dest.suffix.lower() in MP4_LIKE_EXTENSIONS:
        command.extend(["-movflags", "+faststart"])
    command.append(str(dest))
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise UnrecoverableJebError(f"{ffmpeg_path} was not found for safe remux repair") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "no ffmpeg details").strip()
        raise PreflightJebError(f"ffmpeg safe remux failed: {detail}")
    if not dest.exists() or dest.stat().st_size <= 0:
        raise PreflightJebError("ffmpeg safe remux produced no output")


def unique_corrupt_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 10_000):
        candidate = path.with_name(f"{path.stem}.{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise UnrecoverableJebError(f"could not choose unique corrupt path for {path}")


def format_media_preflight_error(report: MediaPreflightReport) -> str:
    failed = report.failed_results
    message = (
        f"media preflight failed for {len(failed)}/{len(report.results)} file(s); no upload started"
    )
    if not failed:
        return message
    first = failed[0]
    issue = first.issues[0] if first.issues else None
    detail = (
        f"{issue.code}: {issue.message}"
        if issue is not None
        else "preflight failed without details"
    )
    return f"{message}: {first.file.label}: {detail}"


def filesystem_listing(*roots: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in roots:
        candidates: Iterator[Path]
        if root.is_dir():
            candidates = (path for path in sorted(root.rglob("*")) if path.is_file())
        elif root.is_file():
            candidates = iter((root,))
        else:
            continue
        for path in candidates:
            normalized = str(path.resolve())
            if normalized in seen:
                continue
            seen.add(normalized)
            stat = path.stat()
            files.append(
                {
                    "path": normalized,
                    "bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
            )
    return {
        "file_count": len(files),
        "bytes": sum(int(item["bytes"]) for item in files),
        "files": files,
    }


def source_removal_challenge(plan: Mapping[str, Any], expires_at: datetime) -> str:
    payload = stable_json(plan).encode("utf-8")
    action = "purge" if bool(plan["purge"]) else "remove"
    return f"{action}-source-{int(expires_at.timestamp())}-{hashlib.sha256(payload).hexdigest()}"


def source_removal_expiry(challenge: str) -> datetime:
    match = SOURCE_REMOVAL_CHALLENGE.fullmatch(challenge)
    if match is None:
        raise SourceRegistryError("invalid source removal challenge")
    return datetime.fromtimestamp(int(match.group(2)), tz=UTC)


def source_removal_is_purge(challenge: str) -> bool:
    match = SOURCE_REMOVAL_CHALLENGE.fullmatch(challenge)
    if match is None:
        raise SourceRegistryError("invalid source removal challenge")
    return match.group(1) == "purge"


def terminate_tus_upload(config: JebIngressConfig, upload_id: str) -> None:
    url = config.tusd_base_url.rstrip("/") + "/" + upload_id
    try:
        response = httpx.delete(
            url,
            headers={"Tus-Resumable": "1.0.0"},
            timeout=10.0,
        )
        if response.status_code != 404:
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise UnrecoverableJebError(f"could not terminate incomplete upload {upload_id}") from exc
    (config.tus_staging_dir / upload_id).unlink(missing_ok=True)
    (config.tus_staging_dir / f"{upload_id}.info").unlink(missing_ok=True)

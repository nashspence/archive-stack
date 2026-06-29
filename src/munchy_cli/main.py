from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, NoReturn

import typer
from pydantic import ValidationError

from munchy.local_files import FileHashCache, LocalFileCandidate, hash_local_file_candidates
from munchy.platform_files import is_platform_cruft_path
from munchy.profile_routing import (
    ProfileRoutingFile,
    profile_routing_plan,
    profile_routing_requires_exiftool,
    profile_routing_requires_probe,
    routing_exiftool_summary,
    routing_file_facts,
    routing_probe_summary,
)
from munchy.profiles import EncodeProfile, ProfileError, load_encode_profiles
from munchy.runner_client import (
    ATTENTION_STYLE,
    DEFAULT_UPLOAD_CHUNK_MIB,
    DEFAULT_UPLOAD_WORKERS,
    ENTITY_ID_STYLE,
    FIELD_STYLE,
    MunchyRunnerClient,
    RunnerInputFile,
    RunnerUploadRequest,
    format_bytes,
    format_job_failure,
    format_job_status_line,
    format_job_summary_line,
    job_finished_cleanly,
    keep_system_awake,
    make_progress_renderer,
    runner_url_setting,
)

RichConsole: Any
RichGroup: Any
RichTable: Any
RichText: Any

try:
    from rich.console import Console as RichConsole
    from rich.console import Group as RichGroup
    from rich.table import Table as RichTable
    from rich.text import Text as RichText
except ModuleNotFoundError:  # pragma: no cover - exercised only in stripped environments
    RichConsole = None
    RichGroup = None
    RichTable = None
    RichText = None


app = typer.Typer(help="Munchy media ingest CLI.")
profile_app = typer.Typer(help="Encode profile operations.")
job_app = typer.Typer(help="Runner job operations.")
routing_app = typer.Typer(help="Profile routing authoring tools.")
app.add_typer(profile_app, name="profile")
app.add_typer(job_app, name="job")
app.add_typer(routing_app, name="routing")

DEFAULT_TASKS = ["archive_video", "qcut_video", "audio_review"]
DEFAULT_AUDIO_TASKS = ["archive_audio"]
DEFAULT_GROUP = "video"
WORKFLOW_MODES = {"archive", "review_only", "collection_preview"}
ARCHIVE_MODES = {"av1_nvenc", "audio", "originals"}
MUNCHY_CONFIG_ENV = "MUNCHY_JOB_CONFIG"
HASH_CACHE_ENV = "MUNCHY_HASH_CACHE"
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


@app.callback()
def munchy_app() -> None:
    """Keep the CLI in group mode so `munchy job ...` stays canonical."""


def _plain_requested() -> bool:
    raw_value = os.getenv("MUNCHY_CLI_PLAIN", "").strip().casefold()
    return raw_value in {"1", "true", "yes", "on"} or os.getenv("TERM") == "dumb"


def _rich_enabled() -> bool:
    return (
        RichConsole is not None
        and RichGroup is not None
        and RichTable is not None
        and RichText is not None
        and not _plain_requested()
    )


def _console() -> Any:
    if RichConsole is None:
        return None
    color_system = "auto" if sys.stdout.isatty() else None
    return RichConsole(file=sys.stdout, color_system=color_system, highlight=False)


def emit(payload: Any, *, json_mode: bool) -> None:
    if json_mode:
        typer.echo(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return
    if isinstance(payload, str):
        typer.echo(payload)
        return
    console = _console()
    if console is None:
        typer.echo(str(payload))
        return
    console.print(payload)


def _load_profiles_or_exit(path: Path) -> dict[str, EncodeProfile]:
    try:
        return load_encode_profiles(path)
    except (OSError, ProfileError, ValidationError) as exc:
        raise typer.BadParameter(str(exc), param_hint=str(path)) from exc


def _exit_runner_error(exc: BaseException) -> NoReturn:
    typer.echo(f"munchy: {exc}", err=True)
    raise typer.Exit(1) from exc


def _styled_text(value: object, style: str) -> Any:
    text = str(value)
    if RichText is None:
        return text
    return RichText(text, style=style)


def _entity_text(value: object) -> Any:
    return _styled_text(value, ENTITY_ID_STYLE)


def _attention_text(value: object) -> Any:
    text = str(value)
    normalized = text.casefold().replace("-", "_")
    if normalized in {"failed", "cancelled", "canceled"} or "error" in normalized:
        return _styled_text(text, ATTENTION_STYLE)
    return text


def _quiet_table(*columns: str) -> Any:
    table = RichTable(box=None, show_edge=False, padding=(0, 2), collapse_padding=True)
    for index, column in enumerate(columns):
        table.add_column(column, no_wrap=index == 0, header_style=FIELD_STYLE)
    return table


def _detail_table() -> Any:
    table = RichTable(
        box=None,
        show_edge=False,
        show_header=False,
        padding=(0, 2),
        collapse_padding=True,
    )
    table.add_column("Field", style=FIELD_STYLE, no_wrap=True)
    table.add_column("Value")
    return table


def _sequence(value: object) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _mapping(value: object, *, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise typer.BadParameter(f"{label} must be a table/object")
    return dict(value)


def _quality(value: int | None) -> str:
    return "default" if value is None else str(value)


def _plain_profiles(path: Path, profiles: Mapping[str, EncodeProfile]) -> str:
    lines = [f"profiles: {path} count={len(profiles)}"]
    for name, profile in profiles.items():
        archive = profile.archive
        lines.append(f"- {name}")
        lines.append(f"  target: {profile.target}")
        lines.append(f"  container: {archive.container}")
        lines.append(f"  archive: {archive.codec} quality={_quality(archive.quality)}")
        if archive.max_height is not None:
            lines.append(f"  max_height: {archive.max_height}")
        if archive.preset:
            lines.append(f"  preset: {archive.preset}")
        audio = archive.audio
        audio_bits: list[str] = [audio.codec]
        if audio.bitrate:
            audio_bits.append(f"bitrate={audio.bitrate}")
        if audio.sample_rate:
            audio_bits.append(f"sample_rate={audio.sample_rate}")
        lines.append("  audio: " + " ".join(audio_bits))
    return "\n".join(lines)


def format_profiles(path: Path, profiles: Mapping[str, EncodeProfile]) -> Any:
    if not _rich_enabled():
        return _plain_profiles(path, profiles)

    table = _quiet_table("Profile", "Target", "Container", "Quality", "Height", "Preset", "Audio")
    for name, profile in profiles.items():
        archive = profile.archive
        audio = archive.audio
        audio_summary = str(audio.codec)
        if audio.bitrate:
            audio_summary = f"{audio_summary} {audio.bitrate}"
        table.add_row(
            _entity_text(name),
            profile.target,
            archive.container,
            _quality(archive.quality),
            "" if archive.max_height is None else str(archive.max_height),
            archive.preset or "",
            audio_summary,
        )
    title = RichText("profiles ", style="bold")
    title.append(path.name, style=ENTITY_ID_STYLE)
    return RichGroup(title, table)


def _job_page_header(payload: Mapping[str, Any]) -> str:
    header = (
        f"jobs page {payload.get('page', 1)}/{payload.get('pages', 0)}  "
        f"per_page={payload.get('per_page', 25)}  "
        f"total={payload.get('total', 0)}  "
        f"sort={payload.get('sort', 'updated_at')}  "
        f"order={payload.get('order', 'desc')}  "
        f"terminal={payload.get('terminal', 'active')}"
    )
    if payload.get("query"):
        header += f"  query={payload.get('query')}"
    filters = _mapping(payload.get("filters"), label="filters")
    active_filters = [f"{key}={value}" for key, value in filters.items() if value is not None]
    if active_filters:
        header += "  " + "  ".join(active_filters)
    return header


def _plain_jobs(payload: Mapping[str, Any]) -> str:
    jobs = [job for job in _sequence(payload.get("jobs")) if isinstance(job, Mapping)]
    lines = [_job_page_header(payload)]
    if not jobs:
        lines.append("- none")
        return "\n".join(lines)
    for job in jobs:
        lines.append(f"- {format_job_summary_line(dict(job))}")
    return "\n".join(lines)


def format_jobs(payload: Mapping[str, Any]) -> Any:
    if not _rich_enabled():
        return _plain_jobs(payload)

    jobs = [job for job in _sequence(payload.get("jobs")) if isinstance(job, Mapping)]
    table = _quiet_table("Job", "Collection", "State", "Phase", "Progress")
    for job in jobs:
        job_id = str(job.get("job_id") or job.get("id") or "unknown")
        table.add_row(
            _entity_text(job_id),
            str(job.get("collection_slug") or ""),
            _attention_text(job.get("state", "unknown")),
            str(job.get("phase") or ""),
            format_job_status_line(dict(job)),
        )
    if not table.rows:
        table.add_row("none", "", "", "", "")
    header = RichText(_job_page_header(payload), style="bold")
    return RichGroup(header, table)


def _plain_job(job: Mapping[str, Any]) -> str:
    lines = [
        f"job: {job.get('job_id', 'unknown')}",
        f"collection: {job.get('collection_slug') or 'unknown'}",
        f"state: {job.get('state', 'unknown')}",
    ]
    if job.get("phase"):
        lines.append(f"phase: {job.get('phase')}")
    if job.get("workflow_mode"):
        lines.append(f"workflow: {job.get('workflow_mode')}")
    if job.get("input_upload_id"):
        lines.append(f"input upload: {job.get('input_upload_id')}")
    lines.append(f"status: {format_job_status_line(dict(job))}")
    return "\n".join(lines)


def format_job(job: Mapping[str, Any]) -> Any:
    if not _rich_enabled():
        return _plain_job(job)

    table = _detail_table()
    table.add_row("job", _entity_text(job.get("job_id", "unknown")))
    table.add_row("collection", str(job.get("collection_slug") or "unknown"))
    table.add_row("state", _attention_text(job.get("state", "unknown")))
    if job.get("phase"):
        table.add_row("phase", str(job.get("phase")))
    if job.get("workflow_mode"):
        table.add_row("workflow", str(job.get("workflow_mode")))
    if job.get("input_upload_id"):
        table.add_row("input upload", str(job.get("input_upload_id")))
    if job.get("created_at"):
        table.add_row("created", str(job.get("created_at")))
    if job.get("updated_at"):
        table.add_row("updated", str(job.get("updated_at")))
    table.add_row("status", format_job_status_line(dict(job)))
    title = RichText("job ", style="bold")
    title.append(str(job.get("job_id", "unknown")), style=ENTITY_ID_STYLE)
    return RichGroup(title, table)


def _safe_id(value: str) -> str:
    text = _SAFE_ID_RE.sub("-", value.strip()).strip("-")
    return text or "munchy-job"


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _normalize_mode(value: str | None, *, default: str, allowed: set[str], label: str) -> str:
    mode = (value or default).strip().casefold().replace("-", "_")
    if mode not in allowed:
        raise typer.BadParameter(
            f"{label} must be one of: " + ", ".join(sorted(allowed)),
            param_hint=label,
        )
    return mode


def _default_tasks_for_archive_mode(archive_mode: str) -> list[str]:
    if archive_mode == "originals":
        return []
    if archive_mode == "audio":
        return list(DEFAULT_AUDIO_TASKS)
    return list(DEFAULT_TASKS)


def _optional_bool(value: str | None, *, label: str) -> bool | None:
    if value is None:
        return None
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise typer.BadParameter(f"{label} must be true or false", param_hint=label)


def _normalize_posix(path: str | PurePosixPath) -> str:
    text = str(path).strip().replace("\\", "/").lstrip("/")
    rel = PurePosixPath(text)
    if not rel.parts or rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        raise typer.BadParameter(f"path is not normalized relative POSIX: {path}")
    return rel.as_posix()


def _join_rel(*parts: str | PurePosixPath | None) -> str:
    out: PurePosixPath | None = None
    for part in parts:
        if part is None:
            continue
        text = str(part).strip()
        if not text:
            continue
        normalized = PurePosixPath(_normalize_posix(text))
        out = normalized if out is None else out / normalized
    if out is None:
        raise typer.BadParameter("target path is empty")
    return _normalize_posix(out)


def _load_toml(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except OSError as exc:
        raise typer.BadParameter(str(exc), param_hint="--config") from exc
    if not isinstance(raw, dict):
        raise typer.BadParameter("config must be a TOML table", param_hint="--config")
    return raw


def _configured_job_defaults(config: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(config.get("job"), Mapping):
        return dict(config["job"])
    if isinstance(config.get("munchy_job_defaults"), Mapping):
        return dict(config["munchy_job_defaults"])
    return {}


def _configured_groups(config: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(config.get("groups"), Mapping):
        return dict(config["groups"])
    if isinstance(config.get("profile_groups"), Mapping):
        return dict(config["profile_groups"])
    return {}


def _configured_profiles(config: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(config.get("profiles"), Mapping):
        return {}
    profiles: dict[str, Any] = {}
    for name, raw_profile in dict(config["profiles"]).items():
        try:
            profile = EncodeProfile.model_validate(raw_profile)
        except ValidationError as exc:
            raise typer.BadParameter(f"profile {name}: {exc}") from exc
        profiles[str(name)] = profile.runner_payload()
    return profiles


def _normalize_group_payload(
    name: str,
    raw_group: object,
    *,
    profiles: Mapping[str, Any],
) -> dict[str, Any]:
    group = _mapping(raw_group, label=f"group {name}")
    archive_mode = _normalize_mode(
        str(group.get("archive_mode") or "av1_nvenc"),
        default="av1_nvenc",
        allowed=ARCHIVE_MODES,
        label="archive_mode",
    )
    default_tasks = _default_tasks_for_archive_mode(archive_mode)
    raw_tasks = group.get("tasks")
    tasks = (
        list(default_tasks)
        if raw_tasks is None
        else [str(task) for task in _sequence(raw_tasks)]
    )
    payload: dict[str, Any] = {
        "archive_mode": archive_mode,
        "tasks": tasks,
    }
    profile_name = str(group.get("profile") or "").strip()
    if profile_name:
        if profile_name not in profiles:
            raise typer.BadParameter(f"group {name} references unknown profile {profile_name!r}")
        payload["encode_profile"] = deepcopy(profiles[profile_name])
    if isinstance(group.get("encode_profile"), Mapping):
        payload["encode_profile"] = deepcopy(dict(group["encode_profile"]))
    metadata_projection = group.get("metadata_projection")
    if metadata_projection is False:
        payload["metadata_projection"] = False
    elif isinstance(metadata_projection, Mapping):
        payload["metadata_projection"] = deepcopy(dict(metadata_projection))
    elif metadata_projection is not None:
        raise typer.BadParameter(
            f"group {name} metadata_projection must be a table or false"
        )
    return payload


def _default_group_payload(group_name: str) -> dict[str, dict[str, Any]]:
    return {
        group_name: {
            "archive_mode": "av1_nvenc",
            "tasks": list(DEFAULT_TASKS),
        }
    }


def _storage_groups(groups: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        name: {
            "archive_mode": _normalize_mode(
                str(group.get("archive_mode") or "av1_nvenc"),
                default="av1_nvenc",
                allowed=ARCHIVE_MODES,
                label="archive_mode",
            ),
            "tasks": [str(task) for task in _sequence(group.get("tasks"))],
        }
        for name, group in groups.items()
    }


def _effective_group(
    *,
    group: str | None,
    groups: Mapping[str, Any],
    structured_routing: bool,
) -> str | None:
    if structured_routing:
        if group:
            raise typer.BadParameter("--group is only valid without profile routing")
        return None
    if group:
        return _normalize_posix(group)
    if len(groups) == 1:
        return next(iter(groups))
    if not groups:
        return DEFAULT_GROUP
    raise typer.BadParameter("--group is required when multiple configured groups exist")


def _discover_candidates(
    source: Path,
    *,
    target_prefix: str | None,
    group: str | None,
) -> list[LocalFileCandidate]:
    if source.is_file():
        sources = [(source, source.name)]
    else:
        sources = [
            (path, path.relative_to(source).as_posix())
            for path in sorted(source.rglob("*"))
            if path.is_file()
            and not is_platform_cruft_path(path.relative_to(source).as_posix())
        ]
    if not sources:
        raise typer.BadParameter(f"{source} has no files to upload", param_hint="SOURCE")

    candidates: list[LocalFileCandidate] = []
    for path, rel in sources:
        stat = path.stat()
        target_path = _join_rel(group, target_prefix, rel)
        candidates.append(
            LocalFileCandidate(
                source=path,
                rel_path=target_path,
                bytes=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
            )
        )
    return candidates


def _default_hash_cache_path() -> Path:
    configured = os.getenv(HASH_CACHE_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".cache" / "munchy" / "file-hashes.sqlite3"


def _hash_files(
    candidates: list[LocalFileCandidate],
    *,
    hash_cache: Path | None,
    use_hash_cache: bool,
) -> list[RunnerInputFile]:
    renderer = make_progress_renderer(include_job=False, title="Munchy Prepare")
    cache_context: FileHashCache | None = None
    if use_hash_cache:
        cache_context = FileHashCache(hash_cache or _default_hash_cache_path())
    if cache_context is None:
        with renderer:
            discovery = hash_local_file_candidates(
                candidates,
                cache=None,
                cache_enabled=False,
                renderer=renderer,
            )
    else:
        with cache_context as cache, renderer:
            discovery = hash_local_file_candidates(candidates, cache=cache, renderer=renderer)
    return [
        RunnerInputFile(
            source=item.source,
            rel_path=item.rel_path,
            bytes=item.bytes,
            sha256=item.sha256,
            filesystem_metadata=item.filesystem_metadata,
        )
        for item in discovery.files
    ]


def _ffprobe_for_routing(path: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "ffprobe failed")[-1000:]
        raise RuntimeError(f"ffprobe failed for {path}: {detail}")
    payload = json.loads(proc.stdout or "{}")
    if not isinstance(payload, dict):
        raise RuntimeError(f"ffprobe returned non-object JSON for {path}")
    return payload


def _exiftool_for_routing(path: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [
            "exiftool",
            "-j",
            "-a",
            "-G1",
            "-s",
            "-ee",
            "-FileName",
            "-FileTypeExtension",
            "-MIMEType",
            "-Make",
            "-Model",
            "-Software",
            "-LensModel",
            "-CameraIdentifier",
            "-CameraDirection",
            "-ImageWidth",
            "-ImageHeight",
            "-Orientation",
            "-DateTimeOriginal",
            "-CreateDate",
            "-CreationDate",
            "-BurstUUID",
            "-ContentIdentifier",
            "-StillImageTime",
            "-CaptureMode",
            "-FullFrameRatePlaybackIntent",
            "-AuxiliaryImageType",
            "-DepthMapImage",
            "-MPImage2",
            str(path),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "exiftool failed")[-1000:]
        raise RuntimeError(f"exiftool failed for {path}: {detail}")
    payload = json.loads(proc.stdout or "[]")
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise RuntimeError(f"exiftool returned no metadata object for {path}")
    return dict(payload[0])


def _routing_plan_files(
    candidates: Sequence[LocalFileCandidate],
    *,
    profile_routing: Mapping[str, Any],
) -> list[ProfileRoutingFile]:
    needs_probe = profile_routing_requires_probe(profile_routing)
    needs_exiftool = profile_routing_requires_exiftool(profile_routing)
    files: list[ProfileRoutingFile] = []
    for item in candidates:
        probe_summary: dict[str, Any] | None = None
        probe_error: str | None = None
        if needs_probe:
            try:
                probe_summary = routing_probe_summary(_ffprobe_for_routing(item.source))
            except Exception as exc:
                probe_error = str(exc)[:1000]
        exiftool_summary: dict[str, Any] | None = None
        facts_error: str | None = None
        if needs_exiftool:
            try:
                exiftool_summary = routing_exiftool_summary(_exiftool_for_routing(item.source))
            except Exception as exc:
                facts_error = str(exc)[:1000]
        files.append(
            ProfileRoutingFile(
                path=item.rel_path,
                bytes=item.bytes,
                probe_summary=probe_summary,
                probe_error=probe_error,
                routing_facts=routing_file_facts(
                    item.rel_path,
                    probe_summary=probe_summary,
                    exiftool_summary=exiftool_summary,
                ),
                facts_error=facts_error,
            )
        )
    return files


def _routing_report_text(plan: Mapping[str, Any]) -> str:
    lines = [
        (
            "routing: "
            f"{'ok' if plan.get('ok') else 'failed'} "
            f"files={plan.get('files_total', 0)} "
            f"matched={plan.get('matched_files', 0)} "
            f"left={plan.get('left_files', 0)} "
            f"unmatched={plan.get('unmatched_files', 0)}"
        )
    ]
    route_counts: dict[str, int] = {}
    group_counts: dict[str, int] = {}
    for item in _sequence(plan.get("matches")):
        if not isinstance(item, Mapping):
            continue
        route_id = str(item.get("route_id") or "")
        group = str(item.get("group") or "")
        route_counts[route_id] = route_counts.get(route_id, 0) + 1
        group_counts[group] = group_counts.get(group, 0) + 1
    if route_counts:
        lines.append("routes:")
        for route_id, count in sorted(route_counts.items()):
            lines.append(f"  {route_id}: {count}")
    if group_counts:
        lines.append("groups:")
        for group, count in sorted(group_counts.items()):
            lines.append(f"  {group}: {count}")
    if plan.get("matches"):
        lines.append("matches:")
        for item in _sequence(plan.get("matches")):
            if not isinstance(item, Mapping):
                continue
            pair = ""
            if item.get("pair_kind"):
                pair = f" pair={item.get('pair_kind')}:{item.get('pair_role') or '-'}"
            lines.append(
                "  "
                f"{item.get('path')} -> {item.get('route_id')} "
                f"group={item.get('group')} out={item.get('collection_rel_path')}{pair}"
            )
    if plan.get("left"):
        lines.append("left:")
        for item in _sequence(plan.get("left")):
            if isinstance(item, Mapping):
                lines.append(f"  {item.get('path')} -> {item.get('route_id')}")
    if plan.get("unmatched"):
        lines.append("unmatched:")
        for item in _sequence(plan.get("unmatched")):
            if isinstance(item, Mapping):
                lines.append(f"  {item.get('path')}: {item.get('reason')}")
    return "\n".join(lines)


def _job_request(
    *,
    source: Path,
    config_path: Path | None,
    collection: str | None,
    collection_timestamp: str | None,
    job_id: str | None,
    upload_id: str | None,
    target_prefix: str | None,
    group: str | None,
    workflow_mode: str | None,
    riverhog_enabled: bool,
    riverhog_disabled: bool,
    upload_workers: int,
    upload_chunk_mib: int,
    hash_cache: Path | None,
    use_hash_cache: bool,
) -> RunnerUploadRequest:
    config = _load_toml(config_path)
    defaults = _configured_job_defaults(config)
    profiles = _configured_profiles(config)
    configured_groups = _configured_groups(config)
    profile_routing = defaults.get("profile_routing")
    profile_routing_payload = profile_routing if isinstance(profile_routing, Mapping) else None
    structured_routing = profile_routing_payload is not None
    effective_group = _effective_group(
        group=group,
        groups=configured_groups,
        structured_routing=structured_routing,
    )
    groups = (
        {
            name: _normalize_group_payload(str(name), raw_group, profiles=profiles)
            for name, raw_group in configured_groups.items()
        }
        if configured_groups
        else _default_group_payload(effective_group or DEFAULT_GROUP)
    )

    collection_slug = str(collection or defaults.get("collection_slug") or "").strip()
    if not collection_slug:
        raise typer.BadParameter("--collection is required", param_hint="--collection")
    timestamp = str(
        collection_timestamp or defaults.get("collection_timestamp") or _timestamp()
    ).strip()
    workflow = _normalize_mode(
        workflow_mode or str(defaults.get("workflow_mode") or "archive"),
        default="archive",
        allowed=WORKFLOW_MODES,
        label="workflow_mode",
    )
    archive_mode = _normalize_mode(
        str(defaults.get("archive_mode") or "av1_nvenc"),
        default="av1_nvenc",
        allowed=ARCHIVE_MODES,
        label="archive_mode",
    )
    default_tasks = _default_tasks_for_archive_mode(archive_mode)
    raw_tasks = defaults.get("tasks")
    tasks = (
        list(default_tasks)
        if raw_tasks is None
        else [str(task) for task in _sequence(raw_tasks)]
    )
    generated_job_id = _safe_id(f"{collection_slug}-{timestamp}")
    final_job_id = str(job_id or defaults.get("job_id") or generated_job_id).strip()
    final_upload_id = str(
        upload_id or defaults.get("upload_id") or defaults.get("input_upload_id") or final_job_id
    ).strip()
    if not final_job_id or not final_upload_id:
        raise typer.BadParameter("job id and upload id must not be blank")

    if riverhog_enabled and riverhog_disabled:
        raise typer.BadParameter("--riverhog and --no-riverhog cannot be combined")
    riverhog = deepcopy(_mapping(defaults.get("riverhog"), label="riverhog"))
    if riverhog_enabled:
        riverhog["enabled"] = True
    if riverhog_disabled:
        riverhog["enabled"] = False
    review_upload = deepcopy(_mapping(defaults.get("review_upload"), label="review_upload"))
    notify = deepcopy(_mapping(defaults.get("notify"), label="notify"))

    candidates = _discover_candidates(
        source,
        target_prefix=target_prefix,
        group=effective_group,
    )
    files = tuple(_hash_files(candidates, hash_cache=hash_cache, use_hash_cache=use_hash_cache))
    storage_hint = {
        "workflow_mode": workflow,
        "archive_mode": archive_mode,
        "tasks": tasks,
        "structured_routing": structured_routing,
        "groups": _storage_groups(groups),
    }
    job_payload: dict[str, Any] = {
        "job_id": final_job_id,
        "input_upload_id": final_upload_id,
        "collection_slug": collection_slug,
        "collection_timestamp": timestamp,
        "workflow_mode": workflow,
        "archive_mode": archive_mode,
        "tasks": tasks,
        "groups": groups,
        "riverhog": riverhog,
        "review_upload": review_upload,
        "notify": notify,
        "cleanup_local_on_success": bool(defaults.get("cleanup_local_on_success", False)),
    }
    if profile_routing_payload is not None:
        job_payload["profile_routing"] = deepcopy(dict(profile_routing_payload))
    return RunnerUploadRequest(
        upload_id=final_upload_id,
        job_id=final_job_id,
        files=files,
        storage_hint=storage_hint,
        job_payload=job_payload,
        upload_workers=upload_workers,
        upload_chunk_mib=upload_chunk_mib,
    )


def _requested_containers(request: RunnerUploadRequest) -> list[str]:
    containers: list[str] = []
    groups = request.job_payload.get("groups")
    if not isinstance(groups, Mapping):
        return containers
    for group in groups.values():
        if not isinstance(group, Mapping):
            continue
        profile = group.get("encode_profile")
        if not isinstance(profile, Mapping):
            continue
        archive = profile.get("archive")
        if not isinstance(archive, Mapping):
            continue
        container = str(archive.get("container") or "").strip()
        if container and container not in containers:
            containers.append(container)
    return containers


@routing_app.command("explain")
def explain_routing(
    source: Annotated[Path, typer.Argument(exists=True, readable=True)],
    config_path: Annotated[
        Path,
        typer.Option("--config", "-c", exists=True, dir_okay=False, readable=True),
    ],
    target_prefix: Annotated[
        str | None,
        typer.Option(
            "--target-prefix",
            help="Optional upload-path prefix to apply before routing.",
        ),
    ] = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Explain how profile routing classifies local files."""

    config = _load_toml(config_path)
    defaults = _configured_job_defaults(config)
    routing = defaults.get("profile_routing")
    if not isinstance(routing, Mapping):
        raise typer.BadParameter(
            "config must define job.profile_routing or munchy_job_defaults.profile_routing",
            param_hint="--config",
        )
    profiles = _configured_profiles(config)
    configured_groups = _configured_groups(config)
    if not configured_groups:
        raise typer.BadParameter(
            "profile routing explain requires explicit groups/profile_groups",
            param_hint="--config",
        )
    groups = {
        str(name): _normalize_group_payload(str(name), raw_group, profiles=profiles)
        for name, raw_group in configured_groups.items()
    }
    prefix = target_prefix
    if prefix is None:
        raw_prefix = defaults.get("target_prefix") or defaults.get("upload_prefix")
        prefix = str(raw_prefix) if raw_prefix else None
    candidates = _discover_candidates(source, target_prefix=prefix, group=None)
    files = _routing_plan_files(candidates, profile_routing=routing)
    plan = profile_routing_plan(routing, files, group_names=set(groups)).as_dict()
    if json_mode:
        emit(plan, json_mode=True)
    else:
        emit(_routing_report_text(plan), json_mode=False)
    if not plan["ok"]:
        raise typer.Exit(1)


def _start_summary(request: RunnerUploadRequest, job: Mapping[str, Any]) -> Any:
    total_bytes = sum(item.bytes for item in request.files)
    if not _rich_enabled():
        return "\n".join(
            [
                f"job: {request.job_id}",
                f"input upload: {request.upload_id}",
                f"files: {len(request.files)}",
                f"bytes: {total_bytes}",
                f"state: {job.get('state', 'unknown')}",
            ]
        )
    table = _detail_table()
    table.add_row("job", _entity_text(request.job_id))
    table.add_row("input upload", request.upload_id)
    table.add_row("files", str(len(request.files)))
    table.add_row("bytes", format_bytes(total_bytes))
    table.add_row("state", _attention_text(job.get("state", "unknown")))
    title = RichText("job start ", style="bold")
    title.append(request.job_id, style=ENTITY_ID_STYLE)
    return RichGroup(title, table)


@profile_app.command("validate")
def validate_profile(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Validate Munchy runner encode profile config."""

    profiles = _load_profiles_or_exit(path)
    payload = {
        "path": str(path),
        "profile_count": len(profiles),
        "profiles": [
            {
                "name": name,
                "target": profile.target,
                "container": profile.archive.container,
                "quality": profile.archive.quality,
            }
            for name, profile in profiles.items()
        ],
        "valid": True,
    }
    if json_mode:
        emit(payload, json_mode=True)
        return
    plural = "profile" if len(profiles) == 1 else "profiles"
    emit(f"{path}: ok ({len(profiles)} {plural})", json_mode=False)


@profile_app.command("show")
def show_profile(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Show normalized Munchy runner encode profile config."""

    profiles = _load_profiles_or_exit(path)
    payload = {
        "path": str(path),
        "profiles": {name: profile.runner_payload() for name, profile in profiles.items()},
    }
    emit(payload if json_mode else format_profiles(path, profiles), json_mode=json_mode)


@job_app.command("start")
def start_job(
    source: Annotated[
        Path,
        typer.Argument(
            exists=True,
            readable=True,
            help="Local file or directory to upload to the runner",
        ),
    ],
    runner_url: Annotated[
        str | None,
        typer.Option("--runner-url", help="Munchy runner URL; defaults to MUNCHY_RUNNER_URL"),
    ] = None,
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            exists=True,
            dir_okay=False,
            readable=True,
            help=f"Munchy job TOML config; defaults to {MUNCHY_CONFIG_ENV}",
        ),
    ] = None,
    collection: Annotated[
        str | None,
        typer.Option("--collection", help="Collection slug for this job"),
    ] = None,
    collection_timestamp: Annotated[
        str | None,
        typer.Option("--timestamp", help="Collection timestamp; defaults to now"),
    ] = None,
    group: Annotated[
        str | None,
        typer.Option("--group", help="Profile group for direct group-path uploads"),
    ] = None,
    target_prefix: Annotated[
        str | None,
        typer.Option("--target-prefix", help="Optional target path prefix inside the upload"),
    ] = None,
    workflow_mode: Annotated[
        str | None,
        typer.Option("--workflow", help="archive, review-only, or collection-preview"),
    ] = None,
    job_id: Annotated[str | None, typer.Option("--job-id", help="Runner job id")] = None,
    upload_id: Annotated[
        str | None,
        typer.Option("--upload-id", help="Runner input upload id"),
    ] = None,
    riverhog_enabled: Annotated[
        bool,
        typer.Option("--riverhog", help="Enable Riverhog handoff for this job"),
    ] = False,
    riverhog_disabled: Annotated[
        bool,
        typer.Option("--no-riverhog", help="Disable Riverhog handoff from config"),
    ] = False,
    upload_workers: Annotated[
        int,
        typer.Option("--upload-workers", min=1, max=128, help="Parallel upload workers"),
    ] = DEFAULT_UPLOAD_WORKERS,
    upload_chunk_mib: Annotated[
        int,
        typer.Option("--upload-chunk-mib", min=1, max=1024, help="Upload chunk size in MiB"),
    ] = DEFAULT_UPLOAD_CHUNK_MIB,
    hash_cache: Annotated[
        Path | None,
        typer.Option("--hash-cache", help=f"Hash cache path; defaults to {HASH_CACHE_ENV}"),
    ] = None,
    no_hash_cache: Annotated[
        bool,
        typer.Option("--no-hash-cache", help="Disable the local file hash cache"),
    ] = False,
    wait: Annotated[
        bool,
        typer.Option("--wait/--no-wait", help="Wait until the job reaches safe completion"),
    ] = True,
    interval: Annotated[float, typer.Option("--interval", min=0.5)] = 10.0,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Upload local media and start a runner job."""

    with keep_system_awake("munchy job start"):
        config_path = config or (
            Path(os.environ[MUNCHY_CONFIG_ENV]) if os.getenv(MUNCHY_CONFIG_ENV) else None
        )
        request = _job_request(
            source=source,
            config_path=config_path,
            collection=collection,
            collection_timestamp=collection_timestamp,
            job_id=job_id,
            upload_id=upload_id,
            target_prefix=target_prefix,
            group=group,
            workflow_mode=workflow_mode,
            riverhog_enabled=riverhog_enabled,
            riverhog_disabled=riverhog_disabled,
            upload_workers=upload_workers,
            upload_chunk_mib=upload_chunk_mib,
            hash_cache=hash_cache,
            use_hash_cache=not no_hash_cache,
        )
        client = MunchyRunnerClient(runner_url_setting(runner_url))
        try:
            client.check_ready(
                str(request.job_payload.get("workflow_mode") or "archive"),
                requested_containers=_requested_containers(request),
            )
            client.create_or_get_input_upload(request)
            job = client.create_job(request)
            if not json_mode:
                emit(_start_summary(request, job), json_mode=False)
            client.upload_files(request)
            if wait:
                job = client.wait_for_job(request.job_id, interval=interval)
                if not job_finished_cleanly(job):
                    typer.echo(format_job_failure(job, label="munchy job"), err=True)
                    raise typer.Exit(1)
            elif not json_mode:
                typer.echo(format_job_status_line(job), err=True)
        except Exception as exc:
            _exit_runner_error(exc)
    emit(job if json_mode else format_job(job), json_mode=json_mode)


@job_app.command("list")
def list_jobs(
    runner_url: Annotated[
        str | None,
        typer.Option("--runner-url", help="Munchy runner URL; defaults to MUNCHY_RUNNER_URL"),
    ] = None,
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=500)] = 25,
    sort: Annotated[str, typer.Option("--sort", help="Sort field")] = "updated_at",
    order: Annotated[str, typer.Option("--order", help="Sort order")] = "desc",
    query: Annotated[
        str | None,
        typer.Option("--query", "-q", help="Search job id, collection, upload, state, phase"),
    ] = None,
    terminal: Annotated[
        str,
        typer.Option("--terminal", help="active, terminal, or all"),
    ] = "active",
    state: Annotated[str | None, typer.Option("--state", help="Filter by job state")] = None,
    workflow_mode: Annotated[
        str | None,
        typer.Option("--workflow", help="Filter by workflow mode"),
    ] = None,
    riverhog: Annotated[
        str | None,
        typer.Option("--riverhog", help="Filter Riverhog-enabled jobs: true or false"),
    ] = None,
    cancel_requested: Annotated[
        str | None,
        typer.Option("--cancel-requested", help="Filter cancel-requested jobs: true or false"),
    ] = None,
    storage_wait: Annotated[
        str | None,
        typer.Option("--storage-wait", help="Filter storage-waiting jobs: true or false"),
    ] = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """List runner jobs."""

    client = MunchyRunnerClient(runner_url_setting(runner_url))
    riverhog_filter = _optional_bool(riverhog, label="--riverhog")
    cancel_requested_filter = _optional_bool(cancel_requested, label="--cancel-requested")
    storage_wait_filter = _optional_bool(storage_wait, label="--storage-wait")
    try:
        payload = client.list_jobs(
            page=page,
            per_page=per_page,
            sort=sort,
            order=order,
            query=query,
            terminal=terminal,
            state=state,
            workflow_mode=workflow_mode,
            riverhog_enabled=riverhog_filter,
            cancel_requested=cancel_requested_filter,
            storage_wait=storage_wait_filter,
        )
    except Exception as exc:
        _exit_runner_error(exc)
    emit(payload if json_mode else format_jobs(payload), json_mode=json_mode)


@job_app.command("show")
def show_job(
    job_id: Annotated[str, typer.Argument(help="Runner job id")],
    runner_url: Annotated[
        str | None,
        typer.Option("--runner-url", help="Munchy runner URL; defaults to MUNCHY_RUNNER_URL"),
    ] = None,
    compact: Annotated[
        bool,
        typer.Option("--compact", help="Request compact runner status"),
    ] = False,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Show runner job details."""

    try:
        job = MunchyRunnerClient(runner_url_setting(runner_url)).get_job(job_id, compact=compact)
    except Exception as exc:
        _exit_runner_error(exc)
    emit(job if json_mode else format_job(job), json_mode=json_mode)


@job_app.command("watch")
def watch_job(
    job_id: Annotated[str, typer.Argument(help="Runner job id")],
    runner_url: Annotated[
        str | None,
        typer.Option("--runner-url", help="Munchy runner URL; defaults to MUNCHY_RUNNER_URL"),
    ] = None,
    interval: Annotated[float, typer.Option("--interval", min=0.5)] = 10.0,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit final JSON")] = False,
) -> None:
    """Watch a runner job until it is safe to delete local sources."""

    try:
        final = MunchyRunnerClient(runner_url_setting(runner_url)).wait_for_job(
            job_id,
            interval=interval,
        )
    except Exception as exc:
        _exit_runner_error(exc)
    if not job_finished_cleanly(final):
        typer.echo(format_job_failure(final, label="munchy job"), err=True)
        raise typer.Exit(1)
    emit(final if json_mode else format_job(final), json_mode=json_mode)


@job_app.command("resume")
def resume_job(
    job_id: Annotated[str, typer.Argument(help="Runner job id")],
    runner_url: Annotated[
        str | None,
        typer.Option("--runner-url", help="Munchy runner URL; defaults to MUNCHY_RUNNER_URL"),
    ] = None,
    wait: Annotated[
        bool,
        typer.Option("--wait", help="Wait for the resumed job to finish"),
    ] = False,
    interval: Annotated[float, typer.Option("--interval", min=0.5)] = 10.0,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Resume a failed or cancelled runner job after repair."""

    client = MunchyRunnerClient(runner_url_setting(runner_url))
    try:
        job = client.resume_job(job_id)
        if wait:
            job = client.wait_for_job(job_id, interval=interval)
            if not job_finished_cleanly(job):
                typer.echo(format_job_failure(job, label="munchy job"), err=True)
                raise typer.Exit(1)
    except Exception as exc:
        _exit_runner_error(exc)
    emit(job if json_mode else format_job(job), json_mode=json_mode)


@job_app.command("cancel")
def cancel_job(
    job_id: Annotated[str, typer.Argument(help="Runner job id")],
    runner_url: Annotated[
        str | None,
        typer.Option("--runner-url", help="Munchy runner URL; defaults to MUNCHY_RUNNER_URL"),
    ] = None,
    cleanup: Annotated[
        bool,
        typer.Option("--cleanup", help="Also clean runner-side artifacts"),
    ] = False,
    wait: Annotated[bool, typer.Option("--wait", help="Wait for cancellation to settle")] = False,
    interval: Annotated[float, typer.Option("--interval", min=0.5)] = 10.0,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Cancel a runner job."""

    client = MunchyRunnerClient(runner_url_setting(runner_url))
    try:
        job = client.cancel_job(job_id, cleanup=cleanup)
        if wait or cleanup:
            job = client.wait_for_job(job_id, interval=interval)
            if cleanup and job.get("state") == "cancelled" and not job.get("cleanup_completed_at"):
                job = client.cancel_job(job_id, cleanup=True)
            if job.get("state") != "cancelled":
                raise RuntimeError(f"job did not cancel cleanly: {format_job_status_line(job)}")
    except Exception as exc:
        _exit_runner_error(exc)
    emit(job if json_mode else format_job(job), json_mode=json_mode)


def main() -> None:
    app()

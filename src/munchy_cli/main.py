from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from pydantic import ValidationError

from munchy.job_authoring import (
    COLLECTION_ARCHIVE_DESTINATIONS,
    HASH_CACHE_ENV,
    MUNCHY_CONFIG_ENV,
    WORKFLOW_MODES,
    MunchyJobAuthoringError,
    build_review_sweep_plan,
    build_runner_upload_request,
    configured_groups,
    configured_job_defaults,
    configured_profiles,
    discover_local_candidates,
    load_munchy_job_config,
    normalize_group_payload,
    requested_archive_containers,
    routing_report_text,
)
from munchy.local_routing import routing_plan_files
from munchy.profile_routing import (
    profile_routing_plan,
)
from munchy.profiles import EncodeProfile, ProfileError, load_encode_profiles
from munchy.runner_client import (
    ATTENTION_STYLE,
    DEFAULT_UPLOAD_CHUNK_MIB,
    DEFAULT_UPLOAD_WORKERS,
    ENTITY_ID_STYLE,
    FIELD_STYLE,
    MunchyRunnerClient,
    RunnerUploadRequest,
    format_bytes,
    format_job_failure,
    format_job_status_line,
    format_job_summary_line,
    job_finished_cleanly,
    keep_system_awake,
    runner_url_setting,
)
from riverhog_core.config_yaml import (
    ConfigError,
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


def _normalize_mode(value: str | None, *, default: str, allowed: set[str], label: str) -> str:
    mode = (value or default).strip().casefold().replace("-", "_")
    if mode not in allowed:
        raise typer.BadParameter(
            f"{label} must be one of: " + ", ".join(sorted(allowed)),
            param_hint=label,
        )
    return mode


def _optional_bool(value: str | None, *, label: str) -> bool | None:
    if value is None:
        return None
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise typer.BadParameter(f"{label} must be true or false", param_hint=label)


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

    try:
        config = load_munchy_job_config(config_path)
    except (ConfigError, MunchyJobAuthoringError) as exc:
        raise typer.BadParameter(str(exc), param_hint="--config") from exc
    defaults = configured_job_defaults(config)
    routing = defaults.get("profile_routing")
    if not isinstance(routing, Mapping):
        raise typer.BadParameter(
            "config must define job.routing",
            param_hint="--config",
        )
    try:
        profiles = configured_profiles(config)
    except MunchyJobAuthoringError as exc:
        raise typer.BadParameter(str(exc), param_hint="--config") from exc
    raw_groups = configured_groups(config)
    if not raw_groups:
        raise typer.BadParameter(
            "profile routing explain requires explicit groups",
            param_hint="--config",
        )
    try:
        groups = {
            str(name): normalize_group_payload(str(name), raw_group, profiles=profiles)
            for name, raw_group in raw_groups.items()
        }
    except MunchyJobAuthoringError as exc:
        raise typer.BadParameter(str(exc), param_hint="--config") from exc
    prefix = target_prefix
    if prefix is None:
        raw_prefix = defaults.get("target_prefix") or defaults.get("upload_prefix")
        prefix = str(raw_prefix) if raw_prefix else None
    try:
        candidates = discover_local_candidates(source, target_prefix=prefix, group=None)
    except MunchyJobAuthoringError as exc:
        raise typer.BadParameter(str(exc), param_hint="SOURCE") from exc
    files = routing_plan_files(candidates, profile_routing=routing)
    plan = profile_routing_plan(routing, files, group_names=set(groups)).as_dict()
    if json_mode:
        emit(plan, json_mode=True)
    else:
        emit(routing_report_text(plan), json_mode=False)
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


def _plain_review_sweep_plan(plan: Mapping[str, Any]) -> str:
    state = "ok" if plan.get("ok") else "failed"
    lines = [
        (
            f"review sweep plan: {state} "
            f"routes={plan.get('routes_total', 0)} "
            f"files={plan.get('files_total', 0)} "
            f"variants={plan.get('variants_total', 0)}"
        )
    ]
    target = _mapping(plan.get("target"), label="target")
    if target.get("destination_template"):
        lines.append(
            f"target: {target.get('method', 'command')} {target.get('destination_template')}"
        )
    for error in _sequence(plan.get("errors")):
        lines.append(f"error: {error}")
    for route in _sequence(plan.get("routes")):
        if not isinstance(route, Mapping):
            continue
        lines.append(
            "- "
            f"{route.get('route_id')} "
            f"group={route.get('group')} "
            f"files={route.get('files', 0)} "
            f"variants={route.get('variants_total', 0)}"
        )
        for variant in _sequence(route.get("variants")):
            if not isinstance(variant, Mapping):
                continue
            suffix = ""
            if variant.get("destination"):
                suffix = f" -> {variant.get('destination')}"
            lines.append(f"  {variant.get('profile_id')}{suffix}")
    return "\n".join(lines)


def format_review_sweep_plan(plan: Mapping[str, Any]) -> Any:
    if not _rich_enabled():
        return _plain_review_sweep_plan(plan)

    state = "ok" if plan.get("ok") else "failed"
    detail = _detail_table()
    detail.add_row("state", _attention_text(state))
    detail.add_row("routes", str(plan.get("routes_total", 0)))
    detail.add_row("files", str(plan.get("files_total", 0)))
    detail.add_row("variants", str(plan.get("variants_total", 0)))
    detail.add_row("run", _entity_text(plan.get("run_id", "")))
    target = _mapping(plan.get("target"), label="target")
    if target.get("destination_template"):
        detail.add_row("target", str(target.get("destination_template")))
    if plan.get("errors"):
        detail.add_row("errors", "; ".join(str(item) for item in _sequence(plan.get("errors"))))

    routes_table = _quiet_table("Route", "Group", "Files", "Variants", "Destinations")
    for route in _sequence(plan.get("routes")):
        if not isinstance(route, Mapping):
            continue
        destinations = [
            str(variant.get("destination") or "")
            for variant in _sequence(route.get("variants"))
            if isinstance(variant, Mapping) and variant.get("destination")
        ]
        destination_text = "\n".join(destinations[:3])
        if len(destinations) > 3:
            destination_text += f"\n... {len(destinations) - 3} more"
        routes_table.add_row(
            _entity_text(route.get("route_id", "")),
            str(route.get("group") or ""),
            str(route.get("files", 0)),
            str(route.get("variants_total", 0)),
            destination_text,
        )
    if not routes_table.rows:
        routes_table.add_row("none", "", "", "", "")
    title = RichText("review sweep plan ", style="bold")
    title.append(state, style=ATTENTION_STYLE if state != "ok" else ENTITY_ID_STYLE)
    return RichGroup(title, detail, routes_table)


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


@job_app.command("plan-review-sweep")
def plan_review_sweep(
    source: Annotated[
        Path,
        typer.Argument(
            exists=True,
            readable=True,
            help="Local file or directory to dry-run against the configured review sweep",
        ),
    ],
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-c",
            exists=True,
            dir_okay=False,
            readable=True,
            help=f"Munchy job YAML config; defaults to {MUNCHY_CONFIG_ENV}",
        ),
    ] = None,
    target_prefix: Annotated[
        str | None,
        typer.Option(
            "--target-prefix",
            help="Optional upload-path prefix to apply before routing.",
        ),
    ] = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Dry-run the configured routed review sweep."""

    config_path = config or (
        Path(os.environ[MUNCHY_CONFIG_ENV]) if os.getenv(MUNCHY_CONFIG_ENV) else None
    )
    if config_path is None:
        raise typer.BadParameter(
            f"--config is required unless {MUNCHY_CONFIG_ENV} is set",
            param_hint="--config",
        )
    try:
        plan = build_review_sweep_plan(
            source=source,
            config_path=config_path,
            target_prefix=target_prefix,
        )
    except (ConfigError, MunchyJobAuthoringError) as exc:
        raise typer.BadParameter(str(exc), param_hint="--config") from exc
    emit(plan if json_mode else format_review_sweep_plan(plan), json_mode=json_mode)
    if not plan.get("ok"):
        raise typer.Exit(1)


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
            help=f"Munchy job YAML config; defaults to {MUNCHY_CONFIG_ENV}",
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
        typer.Option("--workflow", help="collection-archive or review-only"),
    ] = None,
    collection_archive_destination: Annotated[
        str | None,
        typer.Option(
            "--destination",
            help="Collection archive destination: target or riverhog",
        ),
    ] = None,
    job_id: Annotated[str | None, typer.Option("--job-id", help="Runner job id")] = None,
    upload_id: Annotated[
        str | None,
        typer.Option("--upload-id", help="Runner input upload id"),
    ] = None,
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
        try:
            request = build_runner_upload_request(
                source=source,
                config_path=config_path,
                collection=collection,
                collection_timestamp=collection_timestamp,
                job_id=job_id,
                upload_id=upload_id,
                target_prefix=target_prefix,
                group=group,
                workflow_mode=workflow_mode,
                collection_archive_destination=collection_archive_destination,
                upload_workers=upload_workers,
                upload_chunk_mib=upload_chunk_mib,
                hash_cache=hash_cache,
                use_hash_cache=not no_hash_cache,
            )
        except (ConfigError, MunchyJobAuthoringError) as exc:
            raise typer.BadParameter(str(exc)) from exc
        client = MunchyRunnerClient(runner_url_setting(runner_url))
        try:
            client.check_ready(
                str(request.job_payload.get("workflow_mode") or "collection_archive"),
                requested_containers=requested_archive_containers(request),
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
    collection_archive_destination: Annotated[
        str | None,
        typer.Option(
            "--destination",
            help="Filter collection archive destination: target or riverhog",
        ),
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
    destination_filter = (
        _normalize_mode(
            collection_archive_destination,
            default="riverhog",
            allowed=COLLECTION_ARCHIVE_DESTINATIONS,
            label="collection_archive.destination",
        )
        if collection_archive_destination
        else None
    )
    cancel_requested_filter = _optional_bool(cancel_requested, label="--cancel-requested")
    storage_wait_filter = _optional_bool(storage_wait, label="--storage-wait")
    normalized_workflow = (
        _normalize_mode(
            workflow_mode,
            default="collection_archive",
            allowed=WORKFLOW_MODES,
            label="workflow_mode",
        )
        if workflow_mode
        else None
    )
    try:
        payload = client.list_jobs(
            page=page,
            per_page=per_page,
            sort=sort,
            order=order,
            query=query,
            terminal=terminal,
            state=state,
            workflow_mode=normalized_workflow,
            collection_archive_destination=destination_filter,
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

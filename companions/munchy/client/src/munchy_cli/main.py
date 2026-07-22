from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from cli_support.application_keys import (
    format_app_key_created,
    format_app_key_revoked,
    format_app_keys,
    format_apps,
)
from cli_support.output import format_list_ids, json_text
from config_validation import ConfigError
from munchy_api_client.client import (
    ATTENTION_STYLE,
    DEFAULT_UPLOAD_WORKERS,
    ENTITY_ID_STYLE,
    FIELD_STYLE,
    MunchyAdminClient,
    MunchyClient,
    SubmissionUploadRequest,
    format_bytes,
    format_job_failure,
    format_job_status_line,
    format_job_summary_line,
    job_finished_cleanly,
    keep_system_awake,
    server_url_setting,
)
from munchy_api_client.local_routing import routing_plan_files
from munchy_api_client.routing import (
    routing_plan,
)
from munchy_workflows.job_authoring import (
    HANDOFF_DESTINATIONS,
    HASH_CACHE_ENV,
    MUNCHY_CONFIG_ENV,
    WORKFLOW_MODES,
    MunchyJobAuthoringError,
    build_review_sweep_plan,
    build_submission_upload_request,
    configured_groups,
    configured_job_defaults,
    configured_profiles,
    discover_local_candidates,
    load_munchy_job_config,
    load_munchy_job_definition,
    normalize_group_payload,
    routing_report_text,
)
from munchy_workflows.profiles import EncodeProfile, ProfileError, load_encode_profiles
from pydantic import ValidationError
from time_formats import parse_duration
from tus_transport import DEFAULT_TUS_UPLOAD_CHUNK_MIB

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
application_app = typer.Typer(help="Application access.")
app_key_app = typer.Typer(help="Application key management.")
profile_app = typer.Typer(help="Encode profile operations.")
template_app = typer.Typer(help="Server-owned job-template administration.")
job_app = typer.Typer(help="Munchy job operations.")
routing_app = typer.Typer(help="Routing authoring tools.")
application_app.add_typer(app_key_app, name="key")
app.add_typer(application_app, name="app")
app.add_typer(profile_app, name="profile")
app.add_typer(template_app, name="template")
app.add_typer(job_app, name="job")
app.add_typer(routing_app, name="routing")
_CLIENTS: list[Any] = []


def _track_client[ClientT](client: ClientT) -> ClientT:
    _CLIENTS.append(client)
    return client


def _close_clients() -> None:
    while _CLIENTS:
        close = getattr(_CLIENTS.pop(), "close", None)
        if callable(close):
            close()


@app.callback()
def munchy_app(ctx: typer.Context) -> None:
    """Keep the CLI in group mode so `munchy job ...` stays canonical."""

    ctx.call_on_close(_close_clients)


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
        typer.echo(json_text(payload))
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


def _exit_server_error(exc: BaseException) -> NoReturn:
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
    if normalized in {"failed", "canceled"} or "error" in normalized:
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


def _template_inputs(values: list[str]) -> dict[str, str]:
    inputs: dict[str, str] = {}
    for item in values:
        name, separator, value = item.partition("=")
        if not separator or not name.strip() or not value.strip():
            raise typer.BadParameter("--input must use NAME=VALUE", param_hint="--input")
        inputs[name.strip()] = value.strip()
    return inputs


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


def _template_page_header(payload: Mapping[str, Any]) -> str:
    header = (
        f"templates page {payload.get('page', 1)}/{payload.get('pages', 0)}  "
        f"per_page={payload.get('per_page', 25)}  "
        f"total={payload.get('total', 0)}  "
        f"sort={payload.get('sort', 'name')}  "
        f"order={payload.get('order', 'asc')}"
    )
    if payload.get("query"):
        header += f"  query={payload.get('query')}"
    enabled = _mapping(payload.get("filters"), label="filters").get("enabled")
    if enabled is not None:
        header += f"  enabled={str(bool(enabled)).lower()}"
    return header


def format_job_templates(payload: Mapping[str, Any]) -> Any:
    templates = [item for item in _sequence(payload.get("templates")) if isinstance(item, Mapping)]
    if not _rich_enabled():
        lines = [_template_page_header(payload)]
        lines.extend(
            f"- {item.get('name')} revision={item.get('revision')} "
            f"enabled={str(bool(item.get('enabled'))).lower()} digest={item.get('digest')}"
            for item in templates
        )
        if not templates:
            lines.append("- none")
        return "\n".join(lines)
    table = _quiet_table("Template", "Revision", "Enabled", "Digest", "Updated")
    for item in templates:
        table.add_row(
            _entity_text(item.get("name", "")),
            str(item.get("revision", "")),
            str(bool(item.get("enabled"))).lower(),
            str(item.get("digest", ""))[:12],
            str(item.get("updated_at", "")),
        )
    if not table.rows:
        table.add_row("none", "", "", "", "")
    return RichGroup(RichText(_template_page_header(payload), style="bold"), table)


def format_job_template(payload: Mapping[str, Any]) -> Any:
    if not _rich_enabled():
        return "\n".join(
            [
                f"template: {payload.get('name', 'unknown')}",
                f"revision: {payload.get('revision', 'unknown')}",
                f"enabled: {str(bool(payload.get('enabled'))).lower()}",
                f"digest: {payload.get('digest', 'unknown')}",
                f"updated: {payload.get('updated_at', 'unknown')}",
            ]
        )
    table = _detail_table()
    table.add_row("template", _entity_text(payload.get("name", "unknown")))
    table.add_row("revision", str(payload.get("revision", "unknown")))
    table.add_row("enabled", str(bool(payload.get("enabled"))).lower())
    table.add_row("digest", str(payload.get("digest", "unknown")))
    table.add_row("updated", str(payload.get("updated_at", "unknown")))
    return table


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


@routing_app.command("explain")
def explain_routing(
    source: Annotated[Path, typer.Argument(exists=True, readable=True)],
    config_path: Annotated[
        Path,
        typer.Option("--config", "-c", exists=True, dir_okay=False, readable=True),
    ],
    destination_prefix: Annotated[
        str | None,
        typer.Option(
            "--destination-prefix",
            help="Optional upload-path prefix to apply before routing.",
        ),
    ] = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Explain how routing classifies local files."""

    try:
        config = load_munchy_job_config(config_path)
    except (ConfigError, MunchyJobAuthoringError) as exc:
        raise typer.BadParameter(str(exc), param_hint="--config") from exc
    defaults = configured_job_defaults(config)
    routing = defaults.get("routing")
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
            "routing explain requires explicit groups",
            param_hint="--config",
        )
    try:
        groups = {
            str(name): normalize_group_payload(str(name), raw_group, profiles=profiles)
            for name, raw_group in raw_groups.items()
        }
    except MunchyJobAuthoringError as exc:
        raise typer.BadParameter(str(exc), param_hint="--config") from exc
    prefix = destination_prefix
    if prefix is None:
        raw_prefix = defaults.get("destination_prefix")
        prefix = str(raw_prefix) if raw_prefix else None
    try:
        candidates = discover_local_candidates(source, destination_prefix=prefix, group=None)
    except MunchyJobAuthoringError as exc:
        raise typer.BadParameter(str(exc), param_hint="SOURCE") from exc
    files = routing_plan_files(candidates, routing=routing)
    plan = routing_plan(routing, files, group_names=set(groups)).as_dict()
    if json_mode:
        emit(plan, json_mode=True)
    else:
        emit(routing_report_text(plan), json_mode=False)
    if not plan["ok"]:
        raise typer.Exit(1)


def _submission_summary(
    request: SubmissionUploadRequest,
    submission: Mapping[str, Any],
) -> Any:
    total_bytes = sum(item.bytes for item in request.files)
    job = _mapping(submission.get("job"), label="job")
    state = job.get("state", "unknown")
    if not _rich_enabled():
        return "\n".join(
            [
                f"submission: {request.submission_id}",
                f"template: {request.template}",
                f"files: {len(request.files)}",
                f"bytes: {total_bytes}",
                f"state: {state}",
            ]
        )
    table = _detail_table()
    table.add_row("submission", _entity_text(request.submission_id))
    table.add_row("template", request.template)
    table.add_row("files", str(len(request.files)))
    table.add_row("bytes", format_bytes(total_bytes))
    table.add_row("state", _attention_text(state))
    title = RichText("submission ", style="bold")
    title.append(request.submission_id, style=ENTITY_ID_STYLE)
    return RichGroup(title, table)


def _submission_plan_payload(
    request: SubmissionUploadRequest,
    *,
    server_url: str,
    preflight: Mapping[str, Any],
) -> dict[str, Any]:
    template = _mapping(preflight.get("template"), label="template")
    return {
        "dry_run": True,
        "status": "would_submit",
        "server_url": server_url,
        "submission_id": request.submission_id,
        "template": request.template,
        "template_revision": template.get("revision"),
        "template_digest": template.get("digest"),
        "files_total": len(request.files),
        "bytes_total": sum(item.bytes for item in request.files),
        "collection_slug": request.collection_slug,
        "collection_timestamp": request.collection_timestamp,
        "workflow_mode": preflight.get("workflow_mode"),
        "content_inspection": preflight.get("content_inspection"),
        "upload_workers": request.upload_workers,
        "upload_chunk_mib": request.upload_chunk_mib,
    }


def format_submission_plan(payload: Mapping[str, Any]) -> Any:
    rows = [
        ("status", str(payload.get("status", "unknown"))),
        ("server", str(payload.get("server_url", "unknown"))),
        ("submission", str(payload.get("submission_id", "unknown"))),
        ("template", str(payload.get("template", "unknown"))),
        ("template revision", str(payload.get("template_revision") or "unknown")),
        ("workflow", str(payload.get("workflow_mode", "unknown"))),
        ("collection", str(payload.get("collection_slug") or "n/a")),
        ("files", str(payload.get("files_total", 0))),
        ("bytes", format_bytes(int(payload.get("bytes_total", 0) or 0))),
        ("content inspection", str(payload.get("content_inspection") or "unknown")),
    ]
    if not _rich_enabled():
        return "submission dry-run\n" + "\n".join(f"{name}: {value}" for name, value in rows)
    table = _detail_table()
    for name, value in rows:
        table.add_row(name, value)
    return RichGroup(RichText("submission dry-run", style="bold"), table)


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
    handoff = _mapping(plan.get("handoff"), label="handoff")
    if handoff.get("location_template"):
        lines.append(f"handoff: {handoff.get('destination')} {handoff.get('location_template')}")
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
            if variant.get("location"):
                suffix = f" -> {variant.get('location')}"
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
    handoff = _mapping(plan.get("handoff"), label="handoff")
    if handoff.get("location_template"):
        detail.add_row("handoff", str(handoff.get("location_template")))
    if plan.get("errors"):
        detail.add_row("errors", "; ".join(str(item) for item in _sequence(plan.get("errors"))))

    routes_table = _quiet_table("Route", "Group", "Files", "Variants", "Locations")
    for route in _sequence(plan.get("routes")):
        if not isinstance(route, Mapping):
            continue
        locations = [
            str(variant.get("location") or "")
            for variant in _sequence(route.get("variants"))
            if isinstance(variant, Mapping) and variant.get("location")
        ]
        location_text = "\n".join(locations[:3])
        if len(locations) > 3:
            location_text += f"\n... {len(locations) - 3} more"
        routes_table.add_row(
            _entity_text(route.get("route_id", "")),
            str(route.get("group") or ""),
            str(route.get("files", 0)),
            str(route.get("variants_total", 0)),
            location_text,
        )
    if not routes_table.rows:
        routes_table.add_row("none", "", "", "", "")
    title = RichText("review sweep plan ", style="bold")
    title.append(state, style=ATTENTION_STYLE if state != "ok" else ENTITY_ID_STYLE)
    return RichGroup(title, detail, routes_table)


def _job_template_definition(path: Path) -> dict[str, Any]:
    try:
        return load_munchy_job_definition(path)
    except (ConfigError, MunchyJobAuthoringError) as exc:
        raise typer.BadParameter(str(exc), param_hint=str(path)) from exc


def _admin_client(server_url: str | None) -> MunchyAdminClient:
    return _track_client(MunchyAdminClient(server_url_setting(server_url)))


def _server_client(server_url: str | None) -> MunchyClient:
    return _track_client(MunchyClient(server_url_setting(server_url)))


_APP_SORT_FIELDS = {"name", "keys", "active_keys", "last_used_at"}
_APP_KEY_SORT_FIELDS = {"id", "created_at", "expires_at", "last_used_at"}


def _list_order(sort: str, order: str, *, fields: set[str]) -> str:
    if sort not in fields:
        raise typer.BadParameter(
            f"sort must be one of {', '.join(sorted(fields))}",
            param_hint="--sort",
        )
    normalized_order = order.casefold()
    if normalized_order not in {"asc", "desc"}:
        raise typer.BadParameter("order must be asc or desc", param_hint="--order")
    return normalized_order


@application_app.command("list")
def list_applications(
    server_url: Annotated[str | None, typer.Option("--server-url")] = None,
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
    sort: Annotated[str, typer.Option("--sort", help="Sort field")] = "name",
    order: Annotated[str, typer.Option("--order", help="Sort order")] = "asc",
    query: Annotated[str | None, typer.Option("--query", "-q")] = None,
    active: Annotated[
        bool | None,
        typer.Option("--active/--inactive", help="Filter by usable key availability"),
    ] = None,
    all_items: Annotated[bool, typer.Option("--all")] = False,
    ids: Annotated[bool, typer.Option("--ids")] = False,
    json_mode: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List applications with key summaries."""

    if ids and json_mode:
        raise typer.BadParameter("--ids and --json cannot be used together")
    payload = _admin_client(server_url).list_apps(
        page=page,
        per_page=per_page,
        sort=sort,
        order=_list_order(sort, order, fields=_APP_SORT_FIELDS),
        query=query,
        active=active,
        all_items=all_items,
    )
    if ids:
        emit(format_list_ids(payload, "apps", id_key="name"), json_mode=False)
        return
    emit(payload if json_mode else format_apps(payload), json_mode=json_mode)


@app_key_app.command("create")
def create_application_key(
    app_name: Annotated[str, typer.Argument(help="Application name")],
    permission: Annotated[
        list[str],
        typer.Option("--permission", help="Permission to grant; repeat for more than one"),
    ],
    server_url: Annotated[str | None, typer.Option("--server-url")] = None,
    expires_in: Annotated[str | None, typer.Option("--expires-in")] = None,
    json_mode: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Create a key and show its token once."""

    expires_in_seconds: int | None = None
    if expires_in is not None:
        try:
            expires_in_seconds = int(parse_duration(expires_in).total_seconds())
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="--expires-in") from exc
        if expires_in_seconds < 1:
            raise typer.BadParameter(
                "expiry must be at least one second", param_hint="--expires-in"
            )
    payload = _admin_client(server_url).create_app_key(
        app_name,
        permissions=permission,
        expires_in_seconds=expires_in_seconds,
    )
    emit(payload if json_mode else format_app_key_created(payload), json_mode=json_mode)


@app_key_app.command("list")
def list_application_keys(
    app_name: Annotated[str, typer.Argument(help="Application name")],
    server_url: Annotated[str | None, typer.Option("--server-url")] = None,
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
    sort: Annotated[str, typer.Option("--sort", help="Sort field")] = "created_at",
    order: Annotated[str, typer.Option("--order", help="Sort order")] = "desc",
    query: Annotated[str | None, typer.Option("--query", "-q")] = None,
    active: Annotated[
        bool | None,
        typer.Option("--active/--inactive", help="Filter by key usability"),
    ] = None,
    all_items: Annotated[bool, typer.Option("--all")] = False,
    ids: Annotated[bool, typer.Option("--ids")] = False,
    json_mode: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List keys without exposing their tokens."""

    if ids and json_mode:
        raise typer.BadParameter("--ids and --json cannot be used together")
    payload = _admin_client(server_url).list_app_keys(
        app_name,
        page=page,
        per_page=per_page,
        sort=sort,
        order=_list_order(sort, order, fields=_APP_KEY_SORT_FIELDS),
        query=query,
        active=active,
        all_items=all_items,
    )
    if ids:
        emit(format_list_ids(payload, "keys"), json_mode=False)
        return
    emit(payload if json_mode else format_app_keys(payload), json_mode=json_mode)


@app_key_app.command("revoke")
def revoke_application_key(
    app_name: Annotated[str, typer.Argument(help="Application name")],
    key_id: Annotated[str, typer.Argument(help="Key id")],
    server_url: Annotated[str | None, typer.Option("--server-url")] = None,
    json_mode: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Immediately revoke one application key."""

    payload = _admin_client(server_url).revoke_app_key(app_name, key_id)
    emit(payload if json_mode else format_app_key_revoked(payload), json_mode=json_mode)


@template_app.command("list")
def list_job_templates(
    server_url: Annotated[str | None, typer.Option("--server-url")] = None,
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
    sort: Annotated[str, typer.Option("--sort", help="Sort field")] = "name",
    order: Annotated[str, typer.Option("--order", help="Sort order")] = "asc",
    query: Annotated[str | None, typer.Option("--query", "-q")] = None,
    enabled: Annotated[
        bool | None,
        typer.Option("--enabled/--disabled", help="Filter by template availability"),
    ] = None,
    all_items: Annotated[
        bool,
        typer.Option("--all", help="Return every matching job template"),
    ] = False,
    ids: Annotated[
        bool,
        typer.Option("--ids", help="Emit one job-template name per line"),
    ] = False,
    json_mode: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List server-owned job templates."""

    if ids and json_mode:
        raise typer.BadParameter("--ids and --json cannot be used together")
    try:
        payload = _admin_client(server_url).list_job_templates(
            page=page,
            per_page=per_page,
            sort=sort,
            order=order,
            query=query,
            enabled=enabled,
            all_items=all_items,
        )
    except Exception as exc:
        _exit_server_error(exc)
    if ids:
        emit(format_list_ids(payload, "templates", id_key="name"), json_mode=False)
        return
    emit(payload if json_mode else format_job_templates(payload), json_mode=json_mode)


@template_app.command("show")
def show_job_template(
    name: Annotated[str, typer.Argument(help="Job-template name")],
    server_url: Annotated[str | None, typer.Option("--server-url")] = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit full JSON")] = False,
) -> None:
    """Show one server-owned job template."""

    try:
        payload = _admin_client(server_url).get_job_template(name)
    except Exception as exc:
        _exit_server_error(exc)
    emit(payload if json_mode else format_job_template(payload), json_mode=json_mode)


@template_app.command("check")
def check_job_template(
    name: Annotated[str, typer.Argument(help="Job-template name")],
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    server_url: Annotated[str | None, typer.Option("--server-url")] = None,
    json_mode: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Validate an expanded job template without storing it."""

    definition = _job_template_definition(path)
    try:
        payload = _admin_client(server_url).validate_job_template(name, definition)
    except Exception as exc:
        _exit_server_error(exc)
    emit(payload if json_mode else f"{name}: ok ({payload.get('digest')})", json_mode=json_mode)


@template_app.command("create")
def create_job_template(
    name: Annotated[str, typer.Argument(help="Job-template name")],
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    server_url: Annotated[str | None, typer.Option("--server-url")] = None,
    disabled: Annotated[bool, typer.Option("--disabled")] = False,
    json_mode: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Create a server-owned job template."""

    definition = _job_template_definition(path)
    try:
        payload = _admin_client(server_url).create_job_template(
            name,
            definition,
            enabled=not disabled,
        )
    except Exception as exc:
        _exit_server_error(exc)
    emit(payload if json_mode else format_job_template(payload), json_mode=json_mode)


@template_app.command("replace")
def replace_job_template(
    name: Annotated[str, typer.Argument(help="Job-template name")],
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    server_url: Annotated[str | None, typer.Option("--server-url")] = None,
    disabled: Annotated[bool, typer.Option("--disabled")] = False,
    json_mode: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Replace a complete server-owned job template."""

    definition = _job_template_definition(path)
    client = _admin_client(server_url)
    try:
        current = client.get_job_template(name)
        payload = client.replace_job_template(
            name,
            definition,
            expected_revision=int(current["revision"]),
            enabled=not disabled,
        )
    except Exception as exc:
        _exit_server_error(exc)
    emit(payload if json_mode else format_job_template(payload), json_mode=json_mode)


def _set_job_template_enabled(
    name: str,
    *,
    server_url: str | None,
    enabled: bool,
    json_mode: bool,
) -> None:
    client = _admin_client(server_url)
    try:
        current = client.get_job_template(name)
        payload = client.set_job_template_enabled(
            name,
            enabled=enabled,
            expected_revision=int(current["revision"]),
        )
    except Exception as exc:
        _exit_server_error(exc)
    emit(payload if json_mode else format_job_template(payload), json_mode=json_mode)


@template_app.command("enable")
def enable_job_template(
    name: Annotated[str, typer.Argument(help="Job-template name")],
    server_url: Annotated[str | None, typer.Option("--server-url")] = None,
    json_mode: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Enable a job template for new submissions."""

    _set_job_template_enabled(name, server_url=server_url, enabled=True, json_mode=json_mode)


@template_app.command("disable")
def disable_job_template(
    name: Annotated[str, typer.Argument(help="Job-template name")],
    server_url: Annotated[str | None, typer.Option("--server-url")] = None,
    json_mode: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Disable a job template for new submissions."""

    _set_job_template_enabled(name, server_url=server_url, enabled=False, json_mode=json_mode)


@template_app.command("remove")
def remove_job_template(
    name: Annotated[str, typer.Argument(help="Job-template name")],
    server_url: Annotated[str | None, typer.Option("--server-url")] = None,
) -> None:
    """Remove a job template without changing accepted jobs."""

    client = _admin_client(server_url)
    try:
        current = client.get_job_template(name)
        payload = client.delete_job_template(
            name,
            expected_revision=int(current["revision"]),
        )
    except Exception as exc:
        _exit_server_error(exc)
    emit(payload, json_mode=True)


@profile_app.command("validate")
def validate_profile(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Validate Munchy server encode profile config."""

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
    """Show normalized Munchy server encode profile config."""

    profiles = _load_profiles_or_exit(path)
    payload = {
        "path": str(path),
        "profiles": {name: profile.server_payload() for name, profile in profiles.items()},
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
    destination_prefix: Annotated[
        str | None,
        typer.Option(
            "--destination-prefix",
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
            destination_prefix=destination_prefix,
        )
    except (ConfigError, MunchyJobAuthoringError) as exc:
        raise typer.BadParameter(str(exc), param_hint="--config") from exc
    emit(plan if json_mode else format_review_sweep_plan(plan), json_mode=json_mode)
    if not plan.get("ok"):
        raise typer.Exit(1)


@app.command("submit")
def submit(
    source: Annotated[
        Path,
        typer.Argument(
            exists=True,
            readable=True,
            help="Local file or directory to upload",
        ),
    ],
    template: Annotated[
        str,
        typer.Option("--template", help="Server-owned job-template name"),
    ],
    inputs: Annotated[
        list[str] | None,
        typer.Option("--input", help="Template input as NAME=VALUE; repeat as needed"),
    ] = None,
    server_url: Annotated[
        str | None,
        typer.Option("--server-url", help="Munchy server URL; defaults to MUNCHY_BASE_URL"),
    ] = None,
    collection: Annotated[
        str | None,
        typer.Option("--collection", help="Collection slug when required by the template"),
    ] = None,
    collection_timestamp: Annotated[
        str | None,
        typer.Option("--timestamp", help="Collection timestamp; defaults to now"),
    ] = None,
    destination_prefix: Annotated[
        str | None,
        typer.Option("--prefix", help="Optional path prefix inside the submission"),
    ] = None,
    handoff_on_failure: Annotated[
        str,
        typer.Option(
            "--handoff-on-failure",
            help="Handoff handling on failure: preserve-for-resume or cancel",
        ),
    ] = "preserve-for-resume",
    submission_id: Annotated[
        str | None,
        typer.Option("--submission-id", help="Stable submission id; generated by default"),
    ] = None,
    upload_workers: Annotated[
        int,
        typer.Option("--upload-workers", min=1, max=128, help="Parallel upload workers"),
    ] = DEFAULT_UPLOAD_WORKERS,
    upload_chunk_mib: Annotated[
        int,
        typer.Option("--upload-chunk-mib", min=1, max=1024, help="Upload chunk size in MiB"),
    ] = DEFAULT_TUS_UPLOAD_CHUNK_MIB,
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
        typer.Option("--wait/--no-wait", help="Wait until the submission reaches safe completion"),
    ] = True,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Validate the submission without creating or uploading it"),
    ] = False,
    interval: Annotated[float, typer.Option("--interval", min=0.5)] = 10.0,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Submit local files through a server-owned job template."""

    with keep_system_awake("munchy submit"):
        try:
            request = build_submission_upload_request(
                source=source,
                template=template,
                inputs=_template_inputs(inputs or []),
                collection=collection,
                collection_timestamp=collection_timestamp,
                submission_id=submission_id,
                destination_prefix=destination_prefix,
                handoff_on_failure=handoff_on_failure,
                upload_workers=upload_workers,
                upload_chunk_mib=upload_chunk_mib,
                hash_cache=hash_cache,
                use_hash_cache=not no_hash_cache,
            )
        except MunchyJobAuthoringError as exc:
            raise typer.BadParameter(str(exc)) from exc
        resolved_server_url = server_url_setting(server_url)
        client = _track_client(MunchyClient(resolved_server_url))
        try:
            preflight = client.preflight_submission(request)
            if dry_run:
                plan = _submission_plan_payload(
                    request,
                    server_url=resolved_server_url,
                    preflight=preflight,
                )
                emit(plan if json_mode else format_submission_plan(plan), json_mode=json_mode)
                return
            submission = client.create_submission(request)
            if not json_mode:
                emit(_submission_summary(request, submission), json_mode=False)
            client.upload_files(request)
            if wait:
                submission = client.wait_for_submission(request.submission_id, interval=interval)
                job = _mapping(submission.get("job"), label="job")
                if not job_finished_cleanly(job):
                    typer.echo(format_job_failure(job, label="munchy submission"), err=True)
                    raise typer.Exit(1)
            elif not json_mode:
                job = _mapping(submission.get("job"), label="job")
                typer.echo(format_job_status_line(job), err=True)
        except Exception as exc:
            _exit_server_error(exc)
    emit(submission if json_mode else _submission_summary(request, submission), json_mode=json_mode)


@job_app.command("list")
def list_jobs(
    server_url: Annotated[
        str | None,
        typer.Option("--server-url", help="Munchy server URL; defaults to MUNCHY_BASE_URL"),
    ] = None,
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
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
    handoff_destination: Annotated[
        str | None,
        typer.Option(
            "--destination",
            help="Filter handoff destination: command, rclone, or riverhog",
        ),
    ] = None,
    cancel_requested: Annotated[
        bool | None,
        typer.Option(
            "--cancel-requested/--not-cancel-requested",
            help="Filter by whether cancellation was requested",
        ),
    ] = None,
    storage_wait: Annotated[
        bool | None,
        typer.Option(
            "--storage-wait/--no-storage-wait",
            help="Filter by whether storage is pending",
        ),
    ] = None,
    all_items: Annotated[
        bool,
        typer.Option("--all", help="Return every matching job"),
    ] = False,
    ids: Annotated[
        bool,
        typer.Option("--ids", help="Emit one job id per line"),
    ] = False,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """List Munchy jobs."""

    if ids and json_mode:
        raise typer.BadParameter("--ids and --json cannot be used together")
    client = _server_client(server_url)
    destination_filter = (
        _normalize_mode(
            handoff_destination,
            default="riverhog",
            allowed=HANDOFF_DESTINATIONS,
            label="handoff.destination",
        )
        if handoff_destination
        else None
    )
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
            handoff_destination=destination_filter,
            cancel_requested=cancel_requested,
            storage_wait=storage_wait,
            all_items=all_items,
        )
    except Exception as exc:
        _exit_server_error(exc)
    if ids:
        emit(format_list_ids(payload, "jobs", id_key="job_id"), json_mode=False)
        return
    emit(payload if json_mode else format_jobs(payload), json_mode=json_mode)


@job_app.command("show")
def show_job(
    job_id: Annotated[str, typer.Argument(help="Munchy job id")],
    server_url: Annotated[
        str | None,
        typer.Option("--server-url", help="Munchy server URL; defaults to MUNCHY_BASE_URL"),
    ] = None,
    compact: Annotated[
        bool,
        typer.Option("--compact", help="Request compact Munchy status"),
    ] = False,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Show Munchy job details."""

    try:
        job = _server_client(server_url).get_job(job_id, compact=compact)
    except Exception as exc:
        _exit_server_error(exc)
    emit(job if json_mode else format_job(job), json_mode=json_mode)


@job_app.command("watch")
def watch_job(
    job_id: Annotated[str, typer.Argument(help="Munchy job id")],
    server_url: Annotated[
        str | None,
        typer.Option("--server-url", help="Munchy server URL; defaults to MUNCHY_BASE_URL"),
    ] = None,
    interval: Annotated[float, typer.Option("--interval", min=0.5)] = 10.0,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit final JSON")] = False,
) -> None:
    """Watch a Munchy job until it is safe to delete local sources."""

    try:
        final = _server_client(server_url).wait_for_job(
            job_id,
            interval=interval,
        )
    except Exception as exc:
        _exit_server_error(exc)
    if not job_finished_cleanly(final):
        typer.echo(format_job_failure(final, label="munchy job"), err=True)
        raise typer.Exit(1)
    emit(final if json_mode else format_job(final), json_mode=json_mode)


@job_app.command("resume")
def resume_job(
    job_id: Annotated[str, typer.Argument(help="Munchy job id")],
    server_url: Annotated[
        str | None,
        typer.Option("--server-url", help="Munchy server URL; defaults to MUNCHY_BASE_URL"),
    ] = None,
    wait: Annotated[
        bool,
        typer.Option("--wait", help="Wait for the resumed job to finish"),
    ] = False,
    interval: Annotated[float, typer.Option("--interval", min=0.5)] = 10.0,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Resume a failed or canceled Munchy job after repair."""

    client = _server_client(server_url)
    try:
        job = client.resume_job(job_id)
        if wait:
            job = client.wait_for_job(job_id, interval=interval)
            if not job_finished_cleanly(job):
                typer.echo(format_job_failure(job, label="munchy job"), err=True)
                raise typer.Exit(1)
    except Exception as exc:
        _exit_server_error(exc)
    emit(job if json_mode else format_job(job), json_mode=json_mode)


@job_app.command("cancel")
def cancel_job(
    job_id: Annotated[str, typer.Argument(help="Munchy job id")],
    server_url: Annotated[
        str | None,
        typer.Option("--server-url", help="Munchy server URL; defaults to MUNCHY_BASE_URL"),
    ] = None,
    cleanup: Annotated[
        bool,
        typer.Option("--cleanup", help="Also clean server-side artifacts"),
    ] = False,
    wait: Annotated[bool, typer.Option("--wait", help="Wait for cancellation to settle")] = False,
    interval: Annotated[float, typer.Option("--interval", min=0.5)] = 10.0,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Cancel a Munchy job."""

    client = _server_client(server_url)
    try:
        job = client.cancel_job(job_id, cleanup=cleanup)
        if wait or cleanup:
            job = client.wait_for_job(job_id, interval=interval)
            if cleanup and job.get("state") == "canceled" and not job.get("cleanup_completed_at"):
                job = client.cancel_job(job_id, cleanup=True)
            if cleanup and not job.get("cleanup_completed_at"):
                raise RuntimeError(f"job cleanup did not complete: {format_job_status_line(job)}")
            if not cleanup and job.get("state") != "canceled":
                raise RuntimeError(f"job did not cancel cleanly: {format_job_status_line(job)}")
    except Exception as exc:
        _exit_server_error(exc)
    emit(job if json_mode else format_job(job), json_mode=json_mode)


def main() -> None:
    app()

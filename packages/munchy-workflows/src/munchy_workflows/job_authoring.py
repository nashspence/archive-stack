from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any

from config_validation import load_yaml_config, validate_json_schema
from munchy_api_client.client import (
    DEFAULT_UPLOAD_WORKERS,
    SubmissionInputFile,
    SubmissionUploadRequest,
    make_progress_renderer,
)
from munchy_api_client.local_files import (
    FileHashCache,
    LocalFileCandidate,
    hash_local_file_candidates,
)
from munchy_api_client.local_routing import routing_plan_files
from munchy_api_client.routing import routing_plan as build_routing_plan
from munchy_config import (
    MUNCHY_CONFIG_SCHEMA,
    apply_device_profile_to_munchy_config,
    normalize_munchy_job_authoring,
)
from pydantic import ValidationError
from time_formats import utc_now
from tus_transport import DEFAULT_TUS_UPLOAD_CHUNK_MIB

from munchy_workflows.platform_files import is_platform_cruft_path
from munchy_workflows.profiles import EncodeProfile
from munchy_workflows.review_sweep import (
    default_encode_profile_for_output_mode,
    review_output_mode_for_profile,
    review_sweep_variants,
    review_tasks_for_output_mode,
)

DEFAULT_TASKS = ["archive_video", "qcut_video", "audio_review"]
DEFAULT_AUDIO_TASKS = ["archive_audio"]
DEFAULT_GROUP = "video"
WORKFLOW_MODES = {"collection_archive", "review"}
HANDOFF_DESTINATIONS = {"command", "rclone", "riverhog"}
OUTPUT_MODES = {"video", "audio", "preserve"}
HANDOFF_FAILURE_ACTIONS = {"preserve_for_resume", "cancel"}
MUNCHY_CONFIG_ENV = "MUNCHY_JOB_CONFIG"
HASH_CACHE_ENV = "MUNCHY_HASH_CACHE"
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


class MunchyJobAuthoringError(ValueError):
    """Raised when a Munchy job authoring request is invalid."""


def _sequence(value: object) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def mapping(value: object, *, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise MunchyJobAuthoringError(f"{label} must be a table/object")
    return dict(value)


def safe_id(value: str) -> str:
    text = _SAFE_ID_RE.sub("-", value.strip()).strip("-")
    return text or "munchy-job"


def run_id_now() -> str:
    return utc_now().strftime("%Y%m%dT%H%M%S.%fZ")


def normalize_mode(value: str | None, *, default: str, allowed: set[str], label: str) -> str:
    mode = (value or default).strip().casefold().replace("-", "_")
    if mode not in allowed:
        raise MunchyJobAuthoringError(f"{label} must be one of: " + ", ".join(sorted(allowed)))
    return mode


def default_tasks_for_output_mode(output_mode: str) -> list[str]:
    if output_mode == "preserve":
        return []
    if output_mode == "audio":
        return list(DEFAULT_AUDIO_TASKS)
    return list(DEFAULT_TASKS)


def normalize_posix_path(path: str | PurePosixPath) -> str:
    text = str(path).strip().replace("\\", "/").lstrip("/")
    rel = PurePosixPath(text)
    if not rel.parts or rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        raise MunchyJobAuthoringError(f"path is not normalized relative POSIX: {path}")
    return rel.as_posix()


def join_rel_path(*parts: str | PurePosixPath | None) -> str:
    out: PurePosixPath | None = None
    for part in parts:
        if part is None:
            continue
        text = str(part).strip()
        if not text:
            continue
        normalized = PurePosixPath(normalize_posix_path(text))
        out = normalized if out is None else out / normalized
    if out is None:
        raise MunchyJobAuthoringError("target path is empty")
    return normalize_posix_path(out)


def normalize_munchy_config(
    config: Mapping[str, Any],
    *,
    base_path: Path | None = None,
) -> dict[str, Any]:
    normalized = apply_device_profile_to_munchy_config(config, base_path=base_path)
    job = normalized.get("job")
    if isinstance(job, Mapping):
        normalized["job"] = normalize_munchy_job_authoring(job, label="job")
    return normalized


def load_munchy_job_config(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    return normalize_munchy_config(load_munchy_job_definition(path))


def load_munchy_job_definition(path: Path) -> dict[str, Any]:
    raw = load_yaml_config(path)
    validate_json_schema(raw, MUNCHY_CONFIG_SCHEMA, label=str(path))
    expanded = apply_device_profile_to_munchy_config(raw, base_path=path)
    validate_json_schema(expanded, MUNCHY_CONFIG_SCHEMA, label=str(path))
    return expanded


def configured_job_defaults(config: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(config.get("job"), Mapping):
        return dict(config["job"])
    return {}


def configured_groups(config: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(config.get("groups"), Mapping):
        return dict(config["groups"])
    return {}


def configured_profiles(config: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(config.get("profiles"), Mapping):
        return {}
    profiles: dict[str, Any] = {}
    for name, raw_profile in dict(config["profiles"]).items():
        try:
            profile = EncodeProfile.model_validate(raw_profile)
        except ValidationError as exc:
            raise MunchyJobAuthoringError(f"profile {name}: {exc}") from exc
        profiles[str(name)] = profile.server_payload()
    return profiles


def reject_runtime_lifecycle_fields(config: Mapping[str, Any]) -> None:
    job = config.get("job")
    if not isinstance(job, Mapping):
        return
    if "handoff_on_failure" in job:
        raise MunchyJobAuthoringError(
            "handoff_on_failure is a job-start option, not Munchy job config"
        )


def munchy_job_defaults_from_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Lower public Munchy job config into server/Jeb-ready job defaults."""

    reject_runtime_lifecycle_fields(config)
    normalized_config = normalize_munchy_config(config)
    defaults = configured_job_defaults(normalized_config)
    profiles = configured_profiles(normalized_config)
    raw_groups = configured_groups(normalized_config)
    if raw_groups:
        defaults["groups"] = {
            str(name): normalize_group_payload(str(name), raw_group, profiles=profiles)
            for name, raw_group in raw_groups.items()
        }
    return defaults


def normalize_group_payload(
    name: str,
    raw_group: object,
    *,
    profiles: Mapping[str, Any],
) -> dict[str, Any]:
    group = mapping(raw_group, label=f"group {name}")
    output_mode = normalize_mode(
        str(group.get("output_mode") or "video"),
        default="video",
        allowed=OUTPUT_MODES,
        label="output_mode",
    )
    default_tasks = default_tasks_for_output_mode(output_mode)
    raw_tasks = group.get("tasks")
    tasks = (
        list(default_tasks) if raw_tasks is None else [str(task) for task in _sequence(raw_tasks)]
    )
    payload: dict[str, Any] = {
        "output_mode": output_mode,
        "tasks": tasks,
    }
    profile_name = str(group.get("profile") or "").strip()
    if profile_name:
        if profile_name not in profiles:
            raise MunchyJobAuthoringError(
                f"group {name} references unknown profile {profile_name!r}"
            )
        payload["encode_profile"] = deepcopy(profiles[profile_name])
    if isinstance(group.get("encode_profile"), Mapping):
        payload["encode_profile"] = deepcopy(dict(group["encode_profile"]))
    metadata_projection = group.get("metadata_projection")
    if metadata_projection is False:
        payload["metadata_projection"] = False
    elif isinstance(metadata_projection, Mapping):
        payload["metadata_projection"] = deepcopy(dict(metadata_projection))
    elif metadata_projection is not None:
        raise MunchyJobAuthoringError(f"group {name} metadata_projection must be a table or false")
    max_parallel_encodes = group.get("max_parallel_encodes")
    if max_parallel_encodes is not None:
        payload["max_parallel_encodes"] = max_parallel_encodes
    eager_pipeline_batches = group.get("eager_pipeline_batches")
    if eager_pipeline_batches is not None:
        payload["eager_pipeline_batches"] = eager_pipeline_batches
    return payload


def default_group_payload(group_name: str) -> dict[str, dict[str, Any]]:
    return {
        group_name: {
            "output_mode": "video",
            "tasks": list(DEFAULT_TASKS),
        }
    }


def storage_groups(groups: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name, group in groups.items():
        payload: dict[str, Any] = {
            "output_mode": normalize_mode(
                str(group.get("output_mode") or "video"),
                default="video",
                allowed=OUTPUT_MODES,
                label="output_mode",
            ),
            "tasks": [str(task) for task in _sequence(group.get("tasks"))],
        }
        eager_pipeline_batches = group.get("eager_pipeline_batches")
        if eager_pipeline_batches is not None:
            payload["eager_pipeline_batches"] = eager_pipeline_batches
        out[name] = payload
    return out


def review_group_payloads(
    groups: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name, group in groups.items():
        payload = deepcopy(dict(group))
        output_mode = normalize_mode(
            str(payload.get("output_mode") or "video"),
            default="video",
            allowed=OUTPUT_MODES,
            label=f"group {name} output_mode",
        )
        profile = payload.get("encode_profile")
        if isinstance(profile, Mapping):
            output_mode = review_output_mode_for_profile(profile)
        if output_mode == "preserve":
            review_tasks: list[str] = []
        else:
            configured_review_tasks = [
                str(task)
                for task in _sequence(payload.get("tasks"))
                if str(task) in {"qcut_video", "audio_review"}
            ]
            review_tasks = configured_review_tasks or review_tasks_for_output_mode(output_mode)
        payload["output_mode"] = output_mode
        payload["tasks"] = review_tasks
        out[str(name)] = payload
    return out


def grouped_tasks(groups: Mapping[str, Mapping[str, Any]]) -> list[str]:
    tasks: list[str] = []
    for group in groups.values():
        for task in _sequence(group.get("tasks")):
            text = str(task)
            if text not in tasks:
                tasks.append(text)
    return tasks


def effective_group(
    *,
    group: str | None,
    groups: Mapping[str, Any],
    structured_routing: bool,
) -> str | None:
    if structured_routing:
        if group:
            raise MunchyJobAuthoringError("--group is only valid without routing")
        return None
    if group:
        return normalize_posix_path(group)
    if len(groups) == 1:
        return next(iter(groups))
    if not groups:
        return DEFAULT_GROUP
    raise MunchyJobAuthoringError("--group is required when multiple configured groups exist")


def group_base_encode_profile(group: Mapping[str, Any]) -> dict[str, Any]:
    profile = group.get("encode_profile")
    if isinstance(profile, Mapping):
        return deepcopy(dict(profile))
    output_mode = normalize_mode(
        str(group.get("output_mode") or "video"),
        default="video",
        allowed=OUTPUT_MODES,
        label="output_mode",
    )
    return default_encode_profile_for_output_mode(output_mode)


def render_job_template(
    value: str,
    *,
    job: Mapping[str, Any],
    context: Mapping[str, str] | None = None,
) -> str:
    review = mapping(job.get("review"), label="review")
    values = {
        "job_id": str(job.get("job_id") or ""),
        "run_id": str(job.get("run_id") or ""),
        "template_id": str(job.get("template_id") or ""),
        "route_id": str(review.get("route_id") or ""),
        "profile_id": str(review.get("profile_id") or ""),
    }
    if context is not None:
        values.update({str(key): str(value) for key, value in context.items()})
    try:
        return value.format(**values)
    except KeyError as exc:
        raise MunchyJobAuthoringError(f"unknown handoff template field: {exc.args[0]}") from exc


def discover_local_candidates(
    source: Path,
    *,
    destination_prefix: str | None,
    group: str | None,
) -> list[LocalFileCandidate]:
    if source.is_file():
        sources = [(source, source.name)]
    else:
        sources = [
            (path, path.relative_to(source).as_posix())
            for path in sorted(source.rglob("*"))
            if path.is_file() and not is_platform_cruft_path(path.relative_to(source).as_posix())
        ]
    if not sources:
        raise MunchyJobAuthoringError(f"{source} has no files to upload")

    candidates: list[LocalFileCandidate] = []
    for path, rel in sources:
        stat = path.stat()
        target_path = join_rel_path(group, destination_prefix, rel)
        candidates.append(
            LocalFileCandidate(
                source=path,
                rel_path=target_path,
                bytes=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
            )
        )
    return candidates


def default_hash_cache_path() -> Path:
    configured = os.getenv(HASH_CACHE_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".cache" / "munchy" / "file-hashes.sqlite3"


def hash_local_candidates(
    candidates: list[LocalFileCandidate],
    *,
    hash_cache: Path | None,
    use_hash_cache: bool,
) -> list[SubmissionInputFile]:
    renderer = make_progress_renderer(include_job=False, title="Munchy Prepare")
    cache_context: FileHashCache | None = None
    if use_hash_cache:
        cache_context = FileHashCache(hash_cache or default_hash_cache_path())
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
        SubmissionInputFile(
            source=item.source,
            rel_path=item.rel_path,
            bytes=item.bytes,
            sha256=item.sha256,
            filesystem_metadata=item.filesystem_metadata,
        )
        for item in discovery.files
    ]


def build_submission_upload_request(
    *,
    source: Path,
    template_id: str,
    inputs: Mapping[str, str] | None = None,
    run_id: str | None = None,
    submission_id: str | None = None,
    destination_prefix: str | None = None,
    handoff_on_failure: str = "preserve_for_resume",
    upload_workers: int = DEFAULT_UPLOAD_WORKERS,
    upload_chunk_mib: int = DEFAULT_TUS_UPLOAD_CHUNK_MIB,
    hash_cache: Path | None = None,
    use_hash_cache: bool = True,
) -> SubmissionUploadRequest:
    template_name = template_id.strip()
    if not template_name:
        raise MunchyJobAuthoringError("--template must not be blank")
    resolved_run_id = (run_id or run_id_now()).strip()
    identifier = (submission_id or safe_id(f"{template_name}-{resolved_run_id}")).strip()
    if not identifier:
        raise MunchyJobAuthoringError("submission id must not be blank")
    failure_action = normalize_mode(
        handoff_on_failure,
        default="preserve_for_resume",
        allowed=HANDOFF_FAILURE_ACTIONS,
        label="handoff_on_failure",
    )
    candidates = discover_local_candidates(
        source,
        destination_prefix=destination_prefix,
        group=None,
    )
    files = hash_local_candidates(
        candidates,
        hash_cache=hash_cache,
        use_hash_cache=use_hash_cache,
    )
    return SubmissionUploadRequest(
        submission_id=identifier,
        template_id=template_name,
        files=tuple(files),
        inputs={str(name): str(value) for name, value in (inputs or {}).items()},
        run_id=resolved_run_id,
        handoff_on_failure=failure_action,
        upload_workers=upload_workers,
        upload_chunk_mib=upload_chunk_mib,
    )


def _configured_destination_prefix(defaults: Mapping[str, Any], override: str | None) -> str | None:
    if override is not None:
        return override
    raw_prefix = defaults.get("destination_prefix")
    return str(raw_prefix) if raw_prefix else None


def build_review_sweep_plan(
    *,
    source: Path,
    template_id: str,
    config_path: Path | None = None,
    config: Mapping[str, Any] | None = None,
    destination_prefix: str | None = None,
) -> dict[str, Any]:
    normalized_template_id = template_id.strip()
    if not normalized_template_id:
        raise MunchyJobAuthoringError("template_id must not be blank")
    if config_path is not None and config is not None:
        raise MunchyJobAuthoringError("config_path and config are mutually exclusive")
    loaded_config = load_munchy_job_config(config_path) if config_path is not None else config or {}
    normalized_config = normalize_munchy_config(loaded_config)
    defaults = configured_job_defaults(normalized_config)
    workflow = normalize_mode(
        str(defaults.get("workflow_mode") or "collection_archive"),
        default="collection_archive",
        allowed=WORKFLOW_MODES,
        label="workflow_mode",
    )
    if workflow != "review":
        raise MunchyJobAuthoringError("review sweep plans require job.workflow_mode: review")
    review = mapping(defaults.get("review"), label="review")
    sweep = mapping(review.get("sweep"), label="review.sweep")
    if not sweep:
        raise MunchyJobAuthoringError("review sweep plans require job.review.sweep")
    routing = defaults.get("routing")
    if not isinstance(routing, Mapping):
        raise MunchyJobAuthoringError("review sweep plans require job.routing")

    profiles = configured_profiles(normalized_config)
    raw_groups = configured_groups(normalized_config)
    if not raw_groups:
        raise MunchyJobAuthoringError("review sweep plans require explicit groups")
    groups = {
        str(name): normalize_group_payload(str(name), raw_group, profiles=profiles)
        for name, raw_group in raw_groups.items()
    }
    review_groups = review_group_payloads(groups)
    prefix = _configured_destination_prefix(defaults, destination_prefix)
    candidates = discover_local_candidates(source, destination_prefix=prefix, group=None)
    routed_files = routing_plan_files(candidates, routing=routing)
    routing_plan = build_routing_plan(
        routing,
        routed_files,
        group_names=set(groups),
    ).as_dict()

    run_id = str(defaults.get("run_id") or run_id_now()).strip()
    job_id = str(defaults.get("job_id") or safe_id(f"review-{run_id}")).strip()
    job_context = {
        "job_id": job_id,
        "template_id": normalized_template_id,
        "run_id": run_id,
        "review": review,
    }

    route_totals: dict[str, dict[str, Any]] = {}
    for match in routing_plan.get("matches") or []:
        if not isinstance(match, Mapping):
            continue
        action = str(match.get("action") or "upload")
        route_id = str(match.get("route_id") or "").strip()
        group_name = str(match.get("group") or "").strip()
        if not route_id or not group_name:
            continue
        route = route_totals.setdefault(
            route_id,
            {
                "route_id": route_id,
                "group": group_name,
                "files": 0,
                "bytes": 0,
                "evidence_files": 0,
                "evidence_bytes": 0,
            },
        )
        if route["group"] != group_name:
            raise MunchyJobAuthoringError(
                f"review sweep route {route_id!r} resolved to multiple groups: "
                f"{route['group']}, {group_name}"
            )
        bytes_count = int(match.get("bytes") or 0)
        if action == "evidence":
            route["evidence_files"] += 1
            route["evidence_bytes"] += bytes_count
        else:
            route["files"] += 1
            route["bytes"] += bytes_count

    requested_route_ids = [
        str(route_id).strip()
        for route_id in _sequence(sweep.get("route_ids"))
        if str(route_id).strip()
    ]
    requested_route_id_set = set(requested_route_ids)
    handoff = mapping(defaults.get("handoff"), label="handoff")
    handoff_options = mapping(handoff.get("options"), label="handoff.options")
    location_template = str(handoff_options.get("location") or "").strip()
    errors: list[str] = []
    routes: list[dict[str, Any]] = []
    for route_id, route in sorted(route_totals.items()):
        if requested_route_id_set and route_id not in requested_route_id_set:
            continue
        group_name = str(route["group"])
        group = review_groups[group_name]
        tasks = [str(task) for task in _sequence(group.get("tasks"))]
        if not tasks or int(route["files"]) <= 0:
            continue
        variants = review_sweep_variants(
            sweep,
            base_profile=group_base_encode_profile(group),
            route_id=route_id,
        )
        planned_variants: list[dict[str, Any]] = []
        for variant in variants:
            profile_id = str(variant["profile_id"])
            location = None
            if location_template:
                location = render_job_template(
                    location_template,
                    job=job_context,
                    context={"route_id": route_id, "profile_id": profile_id},
                )
            encode_profile = mapping(variant.get("encode_profile"), label="encode_profile")
            planned_variants.append(
                {
                    "profile_id": profile_id,
                    "location": location,
                    "encode_settings": deepcopy(variant.get("encode_settings") or {}),
                    "axis_values": deepcopy(variant.get("axis_values") or {}),
                    "archive": deepcopy(encode_profile.get("archive") or {}),
                }
            )
        routes.append(
            {
                "route_id": route_id,
                "group": group_name,
                "tasks": tasks,
                "files": int(route["files"]),
                "bytes": int(route["bytes"]),
                "evidence_files": int(route["evidence_files"]),
                "evidence_bytes": int(route["evidence_bytes"]),
                "variants": planned_variants,
                "variants_total": len(planned_variants),
            }
        )

    if requested_route_id_set:
        missing = sorted(requested_route_id_set - {route["route_id"] for route in routes})
        if missing:
            errors.append("review sweep route(s) had no reviewable files: " + ", ".join(missing))
    if not routes:
        errors.append("review sweep found no reviewable routes")
    if not routing_plan.get("ok"):
        errors.append(
            "routing did not classify all files: "
            f"unmatched={routing_plan.get('unmatched_files', 0)}"
        )

    variants_total = sum(int(route["variants_total"]) for route in routes)
    files_total = sum(int(route["files"]) for route in routes)
    bytes_total = sum(int(route["bytes"]) for route in routes)
    return {
        "kind": "munchy.review-sweep-plan",
        "schema_version": 1,
        "ok": not errors,
        "source": str(source),
        "destination_prefix": prefix,
        "workflow_mode": "review",
        "job_id": job_id,
        "run_id": run_id,
        "template_id": normalized_template_id,
        "handoff": {
            "destination": str(handoff.get("destination") or ""),
            "location_template": location_template or None,
        },
        "requested_route_ids": requested_route_ids,
        "routes": routes,
        "routes_total": len(routes),
        "files_total": files_total,
        "bytes_total": bytes_total,
        "variants_total": variants_total,
        "routing": {
            "ok": bool(routing_plan.get("ok")),
            "files_total": int(routing_plan.get("files_total") or 0),
            "matched_files": int(routing_plan.get("matched_files") or 0),
            "left_files": int(routing_plan.get("left_files") or 0),
            "unmatched_files": int(routing_plan.get("unmatched_files") or 0),
        },
        "errors": errors,
    }


def routing_report_text(plan: Mapping[str, Any]) -> str:
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


__all__ = [
    "OUTPUT_MODES",
    "HANDOFF_DESTINATIONS",
    "DEFAULT_AUDIO_TASKS",
    "DEFAULT_GROUP",
    "DEFAULT_TASKS",
    "HASH_CACHE_ENV",
    "MUNCHY_CONFIG_ENV",
    "MunchyJobAuthoringError",
    "WORKFLOW_MODES",
    "build_submission_upload_request",
    "build_review_sweep_plan",
    "configured_groups",
    "configured_job_defaults",
    "configured_profiles",
    "default_group_payload",
    "default_hash_cache_path",
    "default_tasks_for_output_mode",
    "discover_local_candidates",
    "effective_group",
    "grouped_tasks",
    "hash_local_candidates",
    "join_rel_path",
    "load_munchy_job_config",
    "mapping",
    "munchy_job_defaults_from_config",
    "normalize_group_payload",
    "normalize_mode",
    "normalize_munchy_config",
    "normalize_posix_path",
    "review_group_payloads",
    "routing_report_text",
    "safe_id",
    "storage_groups",
    "run_id_now",
]

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import ValidationError

from munchy.config_schema import MUNCHY_CONFIG_SCHEMA
from munchy.device_profiles import apply_device_profile_to_munchy_config
from munchy.local_files import FileHashCache, LocalFileCandidate, hash_local_file_candidates
from munchy.local_routing import routing_plan_files
from munchy.platform_files import is_platform_cruft_path
from munchy.profiles import EncodeProfile
from munchy.review_sweep import (
    default_encode_profile_for_output_mode,
    review_output_mode_for_profile,
    review_sweep_variants,
    review_tasks_for_output_mode,
)
from munchy.routing import routing_plan as build_routing_plan
from munchy.runner_client import (
    DEFAULT_UPLOAD_CHUNK_MIB,
    DEFAULT_UPLOAD_WORKERS,
    RunnerInputFile,
    RunnerUploadRequest,
    make_progress_renderer,
)
from riverhog_core.config_yaml import (
    load_yaml_config,
    normalize_munchy_job_authoring,
    validate_json_schema,
)

DEFAULT_TASKS = ["archive_video", "qcut_video", "audio_review"]
DEFAULT_AUDIO_TASKS = ["archive_audio"]
DEFAULT_GROUP = "video"
WORKFLOW_MODES = {"collection_archive", "review"}
COLLECTION_ARCHIVE_DESTINATIONS = {"target", "riverhog"}
OUTPUT_MODES = {"video", "audio", "preserve"}
RIVERHOG_UPLOAD_SESSION_FAILURE_ACTIONS = {"preserve_for_resume", "cancel"}
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


def utc_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


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
    raw = load_yaml_config(path)
    validate_json_schema(raw, MUNCHY_CONFIG_SCHEMA, label=str(path))
    expanded = apply_device_profile_to_munchy_config(raw, base_path=path)
    validate_json_schema(expanded, MUNCHY_CONFIG_SCHEMA, label=str(path))
    return normalize_munchy_config(expanded)


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
        profiles[str(name)] = profile.runner_payload()
    return profiles


def reject_runtime_lifecycle_fields(config: Mapping[str, Any]) -> None:
    job = config.get("job")
    if not isinstance(job, Mapping):
        return
    if "riverhog_upload_session_on_failure" in job:
        raise MunchyJobAuthoringError(
            "riverhog_upload_session_on_failure is a job-start option, not Munchy job config"
        )
    if "riverhog" in job:
        raise MunchyJobAuthoringError(
            "job.riverhog is not Munchy job config; use job.collection_archive.riverhog"
        )
    collection_archive = job.get("collection_archive")
    if not isinstance(collection_archive, Mapping):
        return
    riverhog = collection_archive.get("riverhog")
    if isinstance(riverhog, Mapping) and "upload_session_on_failure" in riverhog:
        raise MunchyJobAuthoringError(
            "collection_archive.riverhog.upload_session_on_failure is a job-start option, "
            "not Munchy job config"
        )


def munchy_job_defaults_from_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Lower public Munchy job config into runner/Jeb-ready job defaults."""

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
        "run_id": str(job.get("run_id") or job.get("collection_timestamp") or ""),
        "device_id": str(review.get("device_id") or ""),
        "route_id": str(review.get("route_id") or ""),
        "profile_id": str(review.get("profile_id") or ""),
        "collection_slug": str(job.get("collection_slug") or ""),
        "collection_timestamp": str(job.get("collection_timestamp") or ""),
    }
    if context is not None:
        values.update({str(key): str(value) for key, value in context.items()})
    try:
        return value.format(**values)
    except KeyError as exc:
        raise MunchyJobAuthoringError(
            f"unknown target upload template field: {exc.args[0]}"
        ) from exc


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
) -> list[RunnerInputFile]:
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
        RunnerInputFile(
            source=item.source,
            rel_path=item.rel_path,
            bytes=item.bytes,
            sha256=item.sha256,
            filesystem_metadata=item.filesystem_metadata,
        )
        for item in discovery.files
    ]


def build_runner_upload_request_from_files(
    files: Sequence[RunnerInputFile],
    *,
    config: Mapping[str, Any] | None = None,
    collection: str | None = None,
    collection_timestamp: str | None = None,
    job_id: str | None = None,
    input_upload_id: str | None = None,
    group: str | None = None,
    workflow_mode: str | None = None,
    collection_archive_destination: str | None = None,
    riverhog_upload_session_on_failure: str | None = None,
    upload_workers: int = DEFAULT_UPLOAD_WORKERS,
    upload_chunk_mib: int = DEFAULT_UPLOAD_CHUNK_MIB,
) -> RunnerUploadRequest:
    normalized_config = normalize_munchy_config(config or {})
    defaults = configured_job_defaults(normalized_config)
    profiles = configured_profiles(normalized_config)
    raw_groups = configured_groups(normalized_config)
    routing = defaults.get("routing")
    routing_payload = routing if isinstance(routing, Mapping) else None
    structured_routing = routing_payload is not None
    selected_group = effective_group(
        group=group,
        groups=raw_groups,
        structured_routing=structured_routing,
    )
    groups = (
        {
            name: normalize_group_payload(str(name), raw_group, profiles=profiles)
            for name, raw_group in raw_groups.items()
        }
        if raw_groups
        else default_group_payload(selected_group or DEFAULT_GROUP)
    )

    workflow = normalize_mode(
        workflow_mode or str(defaults.get("workflow_mode") or "collection_archive"),
        default="collection_archive",
        allowed=WORKFLOW_MODES,
        label="workflow_mode",
    )
    collection_slug = str(collection or defaults.get("collection_slug") or "").strip()
    if workflow == "collection_archive" and not collection_slug:
        raise MunchyJobAuthoringError("--collection is required")
    timestamp = str(
        collection_timestamp or defaults.get("collection_timestamp") or utc_timestamp()
    ).strip()
    run_id = str(defaults.get("run_id") or timestamp).strip()
    raw_collection_archive = deepcopy(
        mapping(defaults.get("collection_archive"), label="collection_archive")
    )
    destination = normalize_mode(
        collection_archive_destination
        or str(raw_collection_archive.get("destination") or "riverhog"),
        default="riverhog",
        allowed=COLLECTION_ARCHIVE_DESTINATIONS,
        label="collection_archive.destination",
    )
    raw_collection_archive["destination"] = destination
    output_mode = normalize_mode(
        str(defaults.get("output_mode") or "video"),
        default="video",
        allowed=OUTPUT_MODES,
        label="output_mode",
    )
    default_tasks = default_tasks_for_output_mode(output_mode)
    raw_tasks = defaults.get("tasks")
    tasks = (
        list(default_tasks) if raw_tasks is None else [str(task) for task in _sequence(raw_tasks)]
    )
    if workflow == "review":
        groups = review_group_payloads(groups)
        grouped = grouped_tasks(groups)
        tasks = grouped or [task for task in tasks if task in {"qcut_video", "audio_review"}]
    generated_job_id = safe_id(f"{collection_slug or workflow}-{timestamp}")
    final_job_id = str(job_id or defaults.get("job_id") or generated_job_id).strip()
    final_input_upload_id = str(
        input_upload_id or defaults.get("input_upload_id") or final_job_id
    ).strip()
    if not final_job_id or not final_input_upload_id:
        raise MunchyJobAuthoringError("job id and input upload id must not be blank")

    review = deepcopy(mapping(defaults.get("review"), label="review"))
    notify = deepcopy(mapping(defaults.get("notify"), label="notify"))

    storage_hint = {
        "workflow_mode": workflow,
        "collection_archive_destination": destination,
        "output_mode": output_mode,
        "tasks": tasks,
        "structured_routing": structured_routing,
        "groups": storage_groups(groups),
    }
    job_payload: dict[str, Any] = {
        "job_id": final_job_id,
        "input_upload_id": final_input_upload_id,
        "run_id": run_id,
        "workflow_mode": workflow,
        "output_mode": output_mode,
        "tasks": tasks,
        "groups": groups,
        "notify": notify,
        "cleanup_local_on_success": bool(defaults.get("cleanup_local_on_success", False)),
    }
    if workflow == "review":
        job_payload["review"] = review
    else:
        job_payload["collection_slug"] = collection_slug
        job_payload["collection_timestamp"] = timestamp
        job_payload["collection_archive"] = raw_collection_archive
    if riverhog_upload_session_on_failure is not None:
        job_payload["riverhog_upload_session_on_failure"] = normalize_mode(
            riverhog_upload_session_on_failure,
            default="preserve_for_resume",
            allowed=RIVERHOG_UPLOAD_SESSION_FAILURE_ACTIONS,
            label="riverhog_upload_session_on_failure",
        )
    if routing_payload is not None:
        job_payload["routing"] = deepcopy(dict(routing_payload))
    return RunnerUploadRequest(
        input_upload_id=final_input_upload_id,
        job_id=final_job_id,
        files=tuple(files),
        storage_hint=storage_hint,
        job_payload=job_payload,
        upload_workers=upload_workers,
        upload_chunk_mib=upload_chunk_mib,
    )


def build_runner_upload_request(
    *,
    source: Path,
    config_path: Path | None = None,
    config: Mapping[str, Any] | None = None,
    collection: str | None = None,
    collection_timestamp: str | None = None,
    job_id: str | None = None,
    input_upload_id: str | None = None,
    destination_prefix: str | None = None,
    group: str | None = None,
    workflow_mode: str | None = None,
    collection_archive_destination: str | None = None,
    riverhog_upload_session_on_failure: str | None = None,
    upload_workers: int = DEFAULT_UPLOAD_WORKERS,
    upload_chunk_mib: int = DEFAULT_UPLOAD_CHUNK_MIB,
    hash_cache: Path | None = None,
    use_hash_cache: bool = True,
) -> RunnerUploadRequest:
    if config_path is not None and config is not None:
        raise MunchyJobAuthoringError("config_path and config are mutually exclusive")
    loaded_config = load_munchy_job_config(config_path) if config_path is not None else config or {}
    normalized_config = normalize_munchy_config(loaded_config)
    defaults = configured_job_defaults(normalized_config)
    raw_groups = configured_groups(normalized_config)
    routing = defaults.get("routing")
    structured_routing = isinstance(routing, Mapping)
    selected_group = effective_group(
        group=group,
        groups=raw_groups,
        structured_routing=structured_routing,
    )
    candidates = discover_local_candidates(
        source,
        destination_prefix=destination_prefix,
        group=selected_group,
    )
    files = hash_local_candidates(
        candidates,
        hash_cache=hash_cache,
        use_hash_cache=use_hash_cache,
    )
    return build_runner_upload_request_from_files(
        files,
        config=normalized_config,
        collection=collection,
        collection_timestamp=collection_timestamp,
        job_id=job_id,
        input_upload_id=input_upload_id,
        group=group,
        workflow_mode=workflow_mode,
        collection_archive_destination=collection_archive_destination,
        riverhog_upload_session_on_failure=riverhog_upload_session_on_failure,
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
    config_path: Path | None = None,
    config: Mapping[str, Any] | None = None,
    destination_prefix: str | None = None,
) -> dict[str, Any]:
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

    timestamp = str(defaults.get("collection_timestamp") or utc_timestamp()).strip()
    run_id = str(defaults.get("run_id") or timestamp).strip()
    collection_slug = str(defaults.get("collection_slug") or "").strip()
    job_id = str(defaults.get("job_id") or safe_id(f"review-{timestamp}")).strip()
    job_context = {
        "job_id": job_id,
        "run_id": run_id,
        "collection_slug": collection_slug,
        "collection_timestamp": timestamp,
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
    target = mapping(review.get("target"), label="review.target")
    destination_template = str(target.get("destination") or "").strip()
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
            destination = None
            if destination_template:
                destination = render_job_template(
                    destination_template,
                    job=job_context,
                    context={"route_id": route_id, "profile_id": profile_id},
                )
            encode_profile = mapping(variant.get("encode_profile"), label="encode_profile")
            planned_variants.append(
                {
                    "profile_id": profile_id,
                    "destination": destination,
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
        "device_id": str(review.get("device_id") or ""),
        "target": {
            "enabled": bool(target.get("enabled", False)),
            "method": str(target.get("method") or "command"),
            "destination_template": destination_template or None,
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


def requested_archive_containers(request: RunnerUploadRequest) -> list[str]:
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
        codec = str(archive.get("codec") or "av1_nvenc")
        container = str(archive.get("container") or ("opus" if codec == "opus" else "mkv"))
        if container and container not in containers:
            containers.append(container)
    return containers


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
    "COLLECTION_ARCHIVE_DESTINATIONS",
    "DEFAULT_AUDIO_TASKS",
    "DEFAULT_GROUP",
    "DEFAULT_TASKS",
    "HASH_CACHE_ENV",
    "MUNCHY_CONFIG_ENV",
    "MunchyJobAuthoringError",
    "WORKFLOW_MODES",
    "build_runner_upload_request",
    "build_runner_upload_request_from_files",
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
    "requested_archive_containers",
    "review_group_payloads",
    "routing_report_text",
    "safe_id",
    "storage_groups",
    "utc_timestamp",
]

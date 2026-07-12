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
from munchy.local_files import FileHashCache, LocalFileCandidate, hash_local_file_candidates
from munchy.platform_files import is_platform_cruft_path
from munchy.profiles import EncodeProfile
from munchy.review_sweep import review_archive_mode_for_profile, review_tasks_for_archive_mode
from munchy.runner_client import (
    DEFAULT_UPLOAD_CHUNK_MIB,
    DEFAULT_UPLOAD_WORKERS,
    RunnerInputFile,
    RunnerUploadRequest,
    make_progress_renderer,
)
from riverhog_core.config_yaml import (
    ConfigError,
    load_yaml_config,
    normalize_munchy_job_authoring,
    validate_json_schema,
)

DEFAULT_TASKS = ["archive_video", "qcut_video", "audio_review"]
DEFAULT_AUDIO_TASKS = ["archive_audio"]
DEFAULT_GROUP = "video"
WORKFLOW_MODES = {"collection_archive", "review"}
COLLECTION_ARCHIVE_DESTINATIONS = {"target", "riverhog"}
ARCHIVE_MODES = {"av1_nvenc", "audio", "preserve"}
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
        raise MunchyJobAuthoringError(
            f"{label} must be one of: " + ", ".join(sorted(allowed))
        )
    return mode


def default_tasks_for_archive_mode(archive_mode: str) -> list[str]:
    if archive_mode == "preserve":
        return []
    if archive_mode == "audio":
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


def normalize_munchy_config(config: Mapping[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(dict(config))
    job = normalized.get("job")
    if isinstance(job, Mapping):
        normalized["job"] = normalize_munchy_job_authoring(job, label="job")
    return normalized


def load_munchy_job_config(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    raw = load_yaml_config(path)
    validate_json_schema(raw, MUNCHY_CONFIG_SCHEMA, label=str(path))
    return normalize_munchy_config(raw)


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


def normalize_group_payload(
    name: str,
    raw_group: object,
    *,
    profiles: Mapping[str, Any],
) -> dict[str, Any]:
    group = mapping(raw_group, label=f"group {name}")
    archive_mode = normalize_mode(
        str(group.get("archive_mode") or "av1_nvenc"),
        default="av1_nvenc",
        allowed=ARCHIVE_MODES,
        label="archive_mode",
    )
    default_tasks = default_tasks_for_archive_mode(archive_mode)
    raw_tasks = group.get("tasks")
    tasks = (
        list(default_tasks) if raw_tasks is None else [str(task) for task in _sequence(raw_tasks)]
    )
    payload: dict[str, Any] = {
        "archive_mode": archive_mode,
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
    return payload


def default_group_payload(group_name: str) -> dict[str, dict[str, Any]]:
    return {
        group_name: {
            "archive_mode": "av1_nvenc",
            "tasks": list(DEFAULT_TASKS),
        }
    }


def storage_groups(groups: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        name: {
            "archive_mode": normalize_mode(
                str(group.get("archive_mode") or "av1_nvenc"),
                default="av1_nvenc",
                allowed=ARCHIVE_MODES,
                label="archive_mode",
            ),
            "tasks": [str(task) for task in _sequence(group.get("tasks"))],
        }
        for name, group in groups.items()
    }


def review_group_payloads(
    groups: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name, group in groups.items():
        payload = deepcopy(dict(group))
        archive_mode = normalize_mode(
            str(payload.get("archive_mode") or "av1_nvenc"),
            default="av1_nvenc",
            allowed=ARCHIVE_MODES,
            label=f"group {name} archive_mode",
        )
        profile = payload.get("encode_profile")
        if isinstance(profile, Mapping):
            archive_mode = review_archive_mode_for_profile(profile)
        if archive_mode == "preserve":
            review_tasks: list[str] = []
        else:
            configured_review_tasks = [
                str(task)
                for task in _sequence(payload.get("tasks"))
                if str(task) in {"qcut_video", "audio_review"}
            ]
            review_tasks = configured_review_tasks or review_tasks_for_archive_mode(archive_mode)
        payload["archive_mode"] = archive_mode
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
            raise MunchyJobAuthoringError("--group is only valid without profile routing")
        return None
    if group:
        return normalize_posix_path(group)
    if len(groups) == 1:
        return next(iter(groups))
    if not groups:
        return DEFAULT_GROUP
    raise MunchyJobAuthoringError("--group is required when multiple configured groups exist")


def discover_local_candidates(
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
            if path.is_file() and not is_platform_cruft_path(path.relative_to(source).as_posix())
        ]
    if not sources:
        raise MunchyJobAuthoringError(f"{source} has no files to upload")

    candidates: list[LocalFileCandidate] = []
    for path, rel in sources:
        stat = path.stat()
        target_path = join_rel_path(group, target_prefix, rel)
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
    upload_id: str | None = None,
    group: str | None = None,
    workflow_mode: str | None = None,
    collection_archive_destination: str | None = None,
    upload_workers: int = DEFAULT_UPLOAD_WORKERS,
    upload_chunk_mib: int = DEFAULT_UPLOAD_CHUNK_MIB,
) -> RunnerUploadRequest:
    normalized_config = normalize_munchy_config(config or {})
    defaults = configured_job_defaults(normalized_config)
    profiles = configured_profiles(normalized_config)
    raw_groups = configured_groups(normalized_config)
    profile_routing = defaults.get("profile_routing")
    profile_routing_payload = profile_routing if isinstance(profile_routing, Mapping) else None
    structured_routing = profile_routing_payload is not None
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
    archive_mode = normalize_mode(
        str(defaults.get("archive_mode") or "av1_nvenc"),
        default="av1_nvenc",
        allowed=ARCHIVE_MODES,
        label="archive_mode",
    )
    default_tasks = default_tasks_for_archive_mode(archive_mode)
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
    final_upload_id = str(
        upload_id or defaults.get("upload_id") or defaults.get("input_upload_id") or final_job_id
    ).strip()
    if not final_job_id or not final_upload_id:
        raise MunchyJobAuthoringError("job id and upload id must not be blank")

    review = deepcopy(mapping(defaults.get("review"), label="review"))
    notify = deepcopy(mapping(defaults.get("notify"), label="notify"))

    storage_hint = {
        "workflow_mode": workflow,
        "collection_archive_destination": destination,
        "archive_mode": archive_mode,
        "tasks": tasks,
        "structured_routing": structured_routing,
        "groups": storage_groups(groups),
    }
    job_payload: dict[str, Any] = {
        "job_id": final_job_id,
        "input_upload_id": final_upload_id,
        "run_id": run_id,
        "workflow_mode": workflow,
        "archive_mode": archive_mode,
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
    if profile_routing_payload is not None:
        job_payload["profile_routing"] = deepcopy(dict(profile_routing_payload))
    return RunnerUploadRequest(
        upload_id=final_upload_id,
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
    upload_id: str | None = None,
    target_prefix: str | None = None,
    group: str | None = None,
    workflow_mode: str | None = None,
    collection_archive_destination: str | None = None,
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
    profile_routing = defaults.get("profile_routing")
    structured_routing = isinstance(profile_routing, Mapping)
    selected_group = effective_group(
        group=group,
        groups=raw_groups,
        structured_routing=structured_routing,
    )
    candidates = discover_local_candidates(
        source,
        target_prefix=target_prefix,
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
        upload_id=upload_id,
        group=group,
        workflow_mode=workflow_mode,
        collection_archive_destination=collection_archive_destination,
        upload_workers=upload_workers,
        upload_chunk_mib=upload_chunk_mib,
    )


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
    "ARCHIVE_MODES",
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
    "configured_groups",
    "configured_job_defaults",
    "configured_profiles",
    "default_group_payload",
    "default_hash_cache_path",
    "default_tasks_for_archive_mode",
    "discover_local_candidates",
    "effective_group",
    "grouped_tasks",
    "hash_local_candidates",
    "join_rel_path",
    "load_munchy_job_config",
    "mapping",
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

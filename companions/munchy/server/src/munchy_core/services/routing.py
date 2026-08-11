from __future__ import annotations

import copy
import hashlib
import json
import logging
import logging.config
import os
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, cast

from munchy_api_client.routing import (
    RoutingFile,
    apply_sidecar_rules,
    exiftool_routing_facts,
    match_route,
    matched_fact_values,
    routing_exiftool_summary,
    routing_exiftool_tags,
    routing_file_facts,
    routing_file_requires_exiftool,
    routing_file_requires_probe,
    routing_plan,
    routing_probe_summary,
    routing_requires_exiftool,
    routing_requires_probe,
    sidecar_exiftool_fact_requests,
    sidecar_rule_exiftool_tags,
    sidecar_rule_fact_extractors,
    sidecar_rules,
)
from munchy_target_support.metadata_projection import (
    MetadataProjectionError,
    ProjectionMetadata,
    immich_xmp_sidecar_path,
    merge_immich_xmp_sidecar,
    project_immich_metadata,
    render_immich_xmp_sidecar,
)
from munchy_workflows.profiles import (
    ArchiveContainer,
)
from riverhog_provenance import parse_journal, validate_journal
from time_formats import (
    utc_timestamp_now,
)

import munchy_core.domain.errors as domain_errors
import munchy_core.domain.models as domain_models
import munchy_core.persistence.sqlite_state as state_store
import munchy_core.runtime.config as runtime_config
import munchy_core.runtime.execution as execution_runtime
import munchy_core.services.handoffs as handoff_service
import munchy_core.services.templates as template_service
import munchy_core.services.uploads as upload_service
from munchy_core.domain.errors import ServiceError

log = logging.getLogger("munchy.server")


def resolve_job_groups(
    upload: dict[str, Any],
    req: domain_models.CreateJobRequest,
) -> dict[str, dict[str, Any]]:
    if req.routing is not None:
        return {name: upload_service.group_dump(group) for name, group in req.groups.items()}
    try:
        input_groups = upload_service.input_upload_groups(upload)
    except ValueError as exc:
        raise ServiceError(status_code=400, detail=str(exc)) from exc
    if req.groups:
        requested = set(req.groups)
        missing = sorted(set(input_groups) - requested)
        extra = sorted(requested - set(input_groups))
        if missing:
            raise ServiceError(
                status_code=400,
                detail=f"missing group config for input directories: {', '.join(missing)}",
            )
        if extra:
            raise ServiceError(
                status_code=400,
                detail=f"group config does not match any input directory: {', '.join(extra)}",
            )
        return {name: upload_service.group_dump(req.groups[name]) for name in input_groups}

    default_group = upload_service.group_dump(upload_service.default_group_config(req))
    return {name: dict(default_group) for name in input_groups}


def ffprobe_for_routing(path: Path) -> dict[str, Any]:
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
        raise domain_errors.RoutingFailed(f"ffprobe failed for {path.name}: {detail}")
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise domain_errors.RoutingFailed(f"ffprobe returned invalid JSON for {path.name}") from exc
    if not isinstance(payload, dict):
        raise domain_errors.RoutingFailed(f"ffprobe returned non-object JSON for {path.name}")
    return payload


def exiftool_for_routing(path: Path, *, tags: Sequence[str] | None = None) -> dict[str, Any]:
    proc = subprocess.run(
        [
            "exiftool",
            "-j",
            "-a",
            "-G1:4",
            "-s",
            "-ee",
            "-c",
            "%.8f",
            *[f"-{tag}" for tag in (tags or routing_exiftool_tags())],
            str(path),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "exiftool failed")[-1000:]
        raise domain_errors.RoutingFailed(f"exiftool failed for {path.name}: {detail}")
    try:
        payload = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise domain_errors.RoutingFailed(
            f"exiftool returned invalid JSON for {path.name}"
        ) from exc
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise domain_errors.RoutingFailed(f"exiftool returned no metadata object for {path.name}")
    return cast(dict[str, Any], payload[0])


def upload_file_data_path(file_state: dict[str, Any]) -> Path:
    tusd_source = upload_service.tusd_data_path(str(file_state["file_upload_id"]))
    if tusd_source.exists():
        return tusd_source
    shared_source = upload_service.shared_input_file_path(file_state)
    if shared_source is not None and shared_source.exists():
        return shared_source
    raise domain_errors.RoutingFailed(
        f"input file data is missing for routing: {file_state.get('path')}"
    )


def routing_needs_exiftool(routing: Mapping[str, Any]) -> bool:
    return routing_requires_exiftool(routing)


def routing_needs_probe(routing: Mapping[str, Any]) -> bool:
    return routing_requires_probe(routing)


def job_routing_file(
    routing: Mapping[str, Any],
    file_state: dict[str, Any],
    *,
    base_routing_facts: Mapping[str, Any] | None = None,
    sidecar_exiftool_tags: Sequence[str] = (),
    sidecar_fact_extractors: Sequence[Mapping[str, Any]] = (),
    sidecar_facts: Mapping[str, Any] | None = None,
    sidecar_facts_error: str | None = None,
) -> RoutingFile:
    rel_path = str(file_state["path"])
    path = upload_file_data_path(file_state)
    base_facts = dict(base_routing_facts or {})
    is_sidecar_evidence = base_facts.get("sidecar.role") == "evidence"
    path_facts = routing_file_facts(rel_path, routing_facts=base_facts)
    probe_summary = None
    if not is_sidecar_evidence and routing_file_requires_probe(routing, path_facts):
        probe_summary = routing_probe_summary(ffprobe_for_routing(path))
    probe_facts = routing_file_facts(
        rel_path,
        probe_summary=probe_summary,
        routing_facts=base_facts,
    )
    exiftool_summary = None
    if not is_sidecar_evidence and routing_file_requires_exiftool(
        routing,
        probe_facts,
    ):
        exiftool_summary = routing_exiftool_summary(
            exiftool_for_routing(path, tags=routing_exiftool_tags(routing))
        )
    collected_sidecar_facts = dict(sidecar_facts) if sidecar_facts is not None else None
    collected_sidecar_facts_error = sidecar_facts_error
    if (
        sidecar_exiftool_tags
        and collected_sidecar_facts is None
        and collected_sidecar_facts_error is None
    ):
        try:
            collected_sidecar_facts = exiftool_routing_facts(
                routing_exiftool_summary(exiftool_for_routing(path, tags=sidecar_exiftool_tags)),
                fact_extractors=sidecar_fact_extractors,
            )
        except domain_errors.RoutingFailed as exc:
            collected_sidecar_facts_error = str(exc)[:1000]
    return RoutingFile(
        path=rel_path,
        bytes=int(file_state.get("bytes") or 0),
        sha256=str(file_state.get("sha256") or "") or None,
        probe_summary=probe_summary,
        routing_facts=routing_file_facts(
            rel_path,
            probe_summary=probe_summary,
            exiftool_summary=exiftool_summary,
            routing_facts=base_facts,
        ),
        sidecar_facts=collected_sidecar_facts,
        sidecar_facts_error=collected_sidecar_facts_error,
    )


def routing_path_facts_for_files(
    routing: Mapping[str, Any],
    file_states: Sequence[dict[str, Any]],
    *,
    sidecar_facts_by_path: Mapping[str, Mapping[str, Any] | None] | None = None,
    sidecar_facts_errors_by_path: Mapping[str, str | None] | None = None,
) -> dict[str, dict[str, Any]]:
    facts_by_path = {
        str(file_state["path"]): routing_file_facts(
            str(file_state["path"]),
            routing_facts={"provenance": file_state_provenance_facts(file_state)},
        )
        for file_state in file_states
    }
    if routing.get("sidecars"):
        return apply_sidecar_rules(
            routing,
            facts_by_path,
            sidecar_facts_by_path=sidecar_facts_by_path,
            sidecar_facts_errors_by_path=sidecar_facts_errors_by_path,
            require_configured_facts=False,
        )
    return facts_by_path


def apply_routing_decision(
    file_state: dict[str, Any],
    decision: Mapping[str, Any],
) -> bool:
    if file_state.get("routed_at"):
        return False
    action = str(decision.get("action") or "upload")
    file_state["route_id"] = str(decision.get("route_id") or "")
    file_state["route_action"] = action
    if decision.get("pair_kind"):
        file_state["pair_kind"] = str(decision["pair_kind"])
    if decision.get("pairing_id"):
        file_state["pair_id"] = str(decision["pairing_id"])
    if decision.get("pair_role"):
        file_state["pair_role"] = str(decision["pair_role"])
    if decision.get("pair_with"):
        file_state["pair_with"] = str(decision["pair_with"])
    if isinstance(decision.get("matched_facts"), dict):
        file_state["route_matched_facts"] = dict(decision["matched_facts"])
    if action == "leave":
        file_state["routed_at"] = utc_timestamp_now()
        return True
    if action == "evidence":
        group = domain_models.validate_group_name(str(decision.get("group") or ""))
        file_state["resolved_group"] = group
        file_state["resolved_group_rel"] = str(
            decision.get("collection_rel_path") or file_state["path"]
        )
        file_state["sidecar_id"] = str(decision.get("sidecar_id") or "")
        file_state["sidecar_format"] = str(decision.get("sidecar_format") or "opaque")
        file_state["sidecar_for"] = str(decision.get("sidecar_for") or "")
        file_state["routed_at"] = utc_timestamp_now()
        return True
    group = domain_models.validate_group_name(str(decision.get("group") or ""))
    file_state["resolved_group"] = group
    file_state["resolved_group_rel"] = str(
        decision.get("collection_rel_path") or file_state["path"]
    )
    file_state["routed_at"] = utc_timestamp_now()
    return True


def route_completed_file(
    job: dict[str, Any],
    file_state: dict[str, Any],
    groups: dict[str, dict[str, Any]],
) -> bool:
    if file_state.get("resolved_group") or file_state.get("route_action") == "leave":
        return False
    routing = job.get("routing")
    if not isinstance(routing, dict):
        return False
    rel_path = str(file_state["path"])

    match = match_route(
        routing,
        rel_path,
        routing_facts=job_routing_file(routing, file_state).routing_facts,
    )
    if match is None:
        raise domain_errors.RoutingFailed(f"routing failed for {rel_path}: no matching route")
    if match.action == "leave":
        return apply_routing_decision(
            file_state,
            {
                "route_id": match.route_id,
                "action": "leave",
                "pair_kind": match.pair_kind,
                "pairing_id": match.pairing_id,
                "pair_role": match.pair_role,
                "pair_with": match.pair_with,
                "matched_facts": matched_fact_values(
                    match.route,
                    match.facts,
                    routing=routing,
                ),
            },
        )
    group = domain_models.validate_group_name(match.group)
    if group not in groups:
        raise domain_errors.RoutingFailed(f"routing failed for {rel_path}: unknown group {group}")
    return apply_routing_decision(
        file_state,
        {
            "route_id": match.route_id,
            "action": match.action,
            "group": group,
            "collection_rel_path": match.collection_rel_path or rel_path,
            "pair_kind": match.pair_kind,
            "pairing_id": match.pairing_id,
            "pair_role": match.pair_role,
            "pair_with": match.pair_with,
            "matched_facts": matched_fact_values(
                match.route,
                match.facts,
                routing=routing,
            ),
        },
    )


def predicate_requires_non_path_facts(predicate: Mapping[str, Any]) -> bool:
    if not predicate:
        return False
    fact = predicate.get("fact")
    if isinstance(fact, str) and not fact.startswith("path."):
        return True
    for key in ("all", "any"):
        items = predicate.get(key)
        if isinstance(items, list) and any(
            isinstance(item, Mapping) and predicate_requires_non_path_facts(item) for item in items
        ):
            return True
    not_item = predicate.get("not")
    if isinstance(not_item, Mapping) and predicate_requires_non_path_facts(not_item):
        return True
    return bool(predicate.get("gate"))


def sidecar_rules_are_path_resolvable(routing: Mapping[str, Any]) -> bool:
    for rule in sidecar_rules(routing):
        for key in ("primary", "sidecar"):
            predicate = rule.get(key)
            if isinstance(predicate, Mapping) and predicate_requires_non_path_facts(predicate):
                return False
    return True


def completed_routing_files_to_route(
    routing: Mapping[str, Any],
    pending_files: Sequence[dict[str, Any]],
    complete_files: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not routing.get("pairings") and not routing.get("sidecars"):
        return list(complete_files)
    if routing.get("pairings"):
        return list(pending_files) if len(complete_files) == len(pending_files) else []
    if not sidecar_rules_are_path_resolvable(routing):
        return list(pending_files) if len(complete_files) == len(pending_files) else []

    complete_paths = {str(file_state["path"]) for file_state in complete_files}
    pending_by_path = {str(file_state["path"]): file_state for file_state in pending_files}
    path_facts_by_path = {path: routing_file_facts(path) for path in pending_by_path}
    sidecar_marked_facts = apply_sidecar_rules(
        routing,
        path_facts_by_path,
        require_configured_facts=False,
    )
    evidence_by_primary: dict[str, set[str]] = {}
    for path, facts in sidecar_marked_facts.items():
        if facts.get("sidecar.role") != "evidence":
            continue
        primary_path = str(facts.get("sidecar.for") or "")
        if primary_path:
            evidence_by_primary.setdefault(primary_path, set()).add(path)

    selected_paths: set[str] = set()
    for file_state in complete_files:
        path = str(file_state["path"])
        facts = sidecar_marked_facts.get(path, {})
        if facts.get("sidecar.role") == "evidence":
            continue
        evidence_paths = evidence_by_primary.get(path, set())
        if any(evidence_path not in complete_paths for evidence_path in evidence_paths):
            continue
        selected_paths.add(path)
        selected_paths.update(evidence_paths)

    return [pending_by_path[path] for path in pending_by_path if path in selected_paths]


def route_completed_input_files(
    job: dict[str, Any],
    upload: dict[str, Any],
    groups: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    upload_id = str(upload["input_upload_id"])
    with execution_runtime.input_upload_state_lock(upload_id):
        return route_completed_input_files_locked(
            job,
            upload_service.load_input_upload(upload_id),
            groups,
        )


def route_completed_input_files_locked(
    job: dict[str, Any],
    upload: dict[str, Any],
    groups: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(job.get("routing"), dict):
        return upload
    changed = False
    pending_files = [
        file_state
        for file_state in upload.get("files", [])
        if not file_state.get("resolved_group") and file_state.get("route_action") != "leave"
    ]
    if not pending_files:
        return upload
    routing = cast(Mapping[str, Any], job["routing"])
    complete_files = [
        file_state
        for file_state in pending_files
        if upload_service.upload_file_status(file_state)["complete"]
    ]
    if not complete_files:
        return upload
    files_to_route = completed_routing_files_to_route(
        routing,
        pending_files,
        complete_files,
    )
    if not files_to_route:
        return upload
    path_facts_by_path = {
        str(file_state["path"]): routing_file_facts(str(file_state["path"]))
        for file_state in files_to_route
    }
    sidecar_fact_requests = sidecar_exiftool_fact_requests(routing, path_facts_by_path)
    sidecar_facts_by_path: dict[str, dict[str, Any]] = {}
    sidecar_facts_errors_by_path: dict[str, str] = {}
    for file_state in files_to_route:
        rel_path = str(file_state["path"])
        sidecar_request = sidecar_fact_requests.get(rel_path)
        if sidecar_request is None or not sidecar_request.tags:
            continue
        try:
            sidecar_facts_by_path[rel_path] = exiftool_routing_facts(
                routing_exiftool_summary(
                    exiftool_for_routing(
                        upload_file_data_path(file_state),
                        tags=sidecar_request.tags,
                    )
                ),
                fact_extractors=sidecar_request.fact_extractors,
            )
        except domain_errors.RoutingFailed as exc:
            sidecar_facts_errors_by_path[rel_path] = str(exc)[:1000]
    base_facts_by_path = routing_path_facts_for_files(
        routing,
        files_to_route,
        sidecar_facts_by_path=sidecar_facts_by_path,
        sidecar_facts_errors_by_path=sidecar_facts_errors_by_path,
    )
    routing_files: list[RoutingFile] = []
    for file_state in files_to_route:
        rel_path = str(file_state["path"])
        sidecar_request = sidecar_fact_requests.get(rel_path)
        routing_files.append(
            job_routing_file(
                routing,
                file_state,
                base_routing_facts=base_facts_by_path.get(rel_path),
                sidecar_exiftool_tags=sidecar_request.tags if sidecar_request else (),
                sidecar_fact_extractors=(
                    sidecar_request.fact_extractors if sidecar_request else ()
                ),
                sidecar_facts=sidecar_facts_by_path.get(rel_path),
                sidecar_facts_error=sidecar_facts_errors_by_path.get(rel_path),
            )
        )
    plan = routing_plan(routing, routing_files, group_names=set(groups))
    if not plan.ok:
        first = plan.unmatched[0] if plan.unmatched else {}
        reason = str(first.get("reason") or "no matching route").replace("_", " ")
        raise domain_errors.RoutingFailed(
            f"routing failed for {first.get('path') or 'input upload'}: {reason}"
        )
    decisions = {item["path"]: item for item in [*plan.matches, *plan.left]}
    for file_state in files_to_route:
        changed = apply_routing_decision(file_state, decisions[str(file_state["path"])]) or changed
    if not changed:
        return upload

    group_counts: dict[str, int] = {}
    route_counts: dict[str, int] = {}
    left_count = 0
    for file_state in upload.get("files", []):
        group = file_state.get("resolved_group")
        route_id = file_state.get("route_id")
        if file_state.get("route_action") == "leave":
            left_count += 1
        if isinstance(group, str) and group:
            group_counts[group] = group_counts.get(group, 0) + 1
        if isinstance(route_id, str) and route_id:
            route_counts[route_id] = route_counts.get(route_id, 0) + 1
    job["routing_result"] = {
        "updated_at": utc_timestamp_now(),
        "files": sum(group_counts.values()),
        "left_files": left_count,
        "groups": group_counts,
        "routes": route_counts,
    }
    adapter = handoff_service.optional_handoff_adapter(job)
    job["handoff_expected_primary_files_total"] = (
        adapter.expected_primary_files_total(upload, groups, None) if adapter is not None else 0
    ) or 0
    upload = upload_service.save_input_upload_raw(upload)
    state_store.save_job(job)
    return upload_service.refresh_input_upload(upload)


def grouped_task_union(groups: dict[str, dict[str, Any]]) -> list[domain_models.TaskName]:
    tasks: list[domain_models.TaskName] = []
    for group in groups.values():
        for task in group.get("tasks") or []:
            if task not in tasks:
                tasks.append(task)
    return tasks


def gpu_group_job_id(job_id: str, group_name: str) -> str:
    digest = hashlib.sha256(f"{job_id}/{group_name}".encode()).hexdigest()[:10]
    safe_group = group_name[:48]
    suffix = f"__{safe_group}__{digest}"
    return f"{job_id[: max(1, 180 - len(suffix))]}{suffix}"


def gpu_eager_batch_job_id(job_id: str, batch_id: str) -> str:
    digest = hashlib.sha256(f"{job_id}/eager/{batch_id}".encode()).hexdigest()[:10]
    safe_batch = batch_id[:48]
    suffix = f"__eager__{safe_batch}__{digest}"
    return f"{job_id[: max(1, 180 - len(suffix))]}{suffix}"


def gpu_job_work_roots(job: dict[str, Any]) -> list[Path]:
    job_id = str(job["job_id"])
    roots: list[Path] = []
    seen: set[Path] = set()

    def add_gpu_job_root(gpu_job_id: str) -> None:
        if not gpu_job_id:
            return
        root = runtime_config.GPU_RUNTIME_DIR / "jobs" / gpu_job_id
        if root in seen:
            return
        seen.add(root)
        roots.append(root)

    add_gpu_job_root(job_id)
    jobs_root = runtime_config.GPU_RUNTIME_DIR / "jobs"
    if jobs_root.exists():
        for root in jobs_root.iterdir():
            if root.name.startswith(f"{job_id}__"):
                add_gpu_job_root(root.name)
    groups = job.get("groups")
    if isinstance(groups, dict):
        for group_name in groups:
            add_gpu_job_root(gpu_group_job_id(job_id, str(group_name)))
    eager = job.get("eager_archive")
    batches = eager.get("batches") if isinstance(eager, dict) else None
    if isinstance(batches, dict):
        for batch_key, batch in batches.items():
            if not isinstance(batch, dict):
                continue
            gpu_job_id = str(batch.get("gpu_job_id") or "")
            add_gpu_job_root(gpu_job_id)
            payload = batch.get("payload")
            if isinstance(payload, dict):
                add_gpu_job_root(str(payload.get("job_id") or ""))
            batch_id = str(batch.get("batch_id") or batch_key)
            add_gpu_job_root(gpu_eager_batch_job_id(job_id, batch_id))
    return roots


def group_archive_container(group_config: dict[str, Any]) -> ArchiveContainer:
    profile = group_config.get("encode_profile")
    archive: dict[str, Any] = {}
    if isinstance(profile, dict) and isinstance(profile.get("archive"), dict):
        archive = profile["archive"]
    output_mode = domain_models.normalize_output_mode(
        str(group_config.get("output_mode") or "video")
    )
    default_container = "opus" if output_mode == "audio" else "mkv"
    container = str(archive.get("container") or default_container)
    if container not in {"mkv", "webm", "opus"}:
        raise RuntimeError(f"unsupported archive container: {container}")
    return container  # type: ignore[return-value]


def archive_container_suffix(group_config: dict[str, Any]) -> str:
    return f".{group_archive_container(group_config)}"


def archive_output_for_upload_file(
    file_state: dict[str, Any],
    *,
    group_name: str,
    group_config: dict[str, Any],
    archive_dir: Path,
) -> Path:
    group_rel = upload_service.upload_file_group_rel_for_state(file_state, group_name)
    return (archive_dir / group_name / group_rel).with_suffix(
        archive_container_suffix(group_config)
    )


def eager_archive_executor(group_config: dict[str, Any]) -> str | None:
    output_mode = domain_models.normalize_output_mode(
        str(group_config.get("output_mode") or "video")
    )
    tasks = set(str(task) for task in group_config.get("tasks") or [])
    if output_mode == "video" and tasks == {"archive_video"}:
        return "gpu"
    if output_mode == "audio" and tasks == {"archive_audio"}:
        return "local_audio"
    return None


def group_is_eager_archive_only(group_config: dict[str, Any]) -> bool:
    return eager_archive_executor(group_config) is not None


def group_produces_primary_archive_output(group_config: dict[str, Any]) -> bool:
    if (
        domain_models.normalize_output_mode(str(group_config.get("output_mode") or "video"))
        == "preserve"
    ):
        return True
    tasks = set(str(task) for task in group_config.get("tasks") or [])
    return bool(tasks & {"archive_video", "archive_audio"})


def archive_output_path_for_routed_file(
    file_state: dict[str, Any],
    *,
    group_name: str,
    group_config: dict[str, Any],
    archive_dir: Path,
) -> Path:
    if (
        domain_models.normalize_output_mode(str(group_config.get("output_mode") or "video"))
        == "preserve"
    ):
        return (
            archive_dir
            / group_name
            / upload_service.upload_file_group_rel_for_state(file_state, group_name)
        )
    return archive_output_for_upload_file(
        file_state,
        group_name=group_name,
        group_config=group_config,
        archive_dir=archive_dir,
    )


def routing_manifest_output_entry(path: Path, *, archive_dir: Path) -> dict[str, Any]:
    rel_path = path.relative_to(archive_dir).as_posix()
    entry: dict[str, Any] = {
        "path": rel_path,
        "exists": path.exists(),
    }
    if path.exists():
        entry["bytes"] = path.stat().st_size
    xmp_sidecar = immich_xmp_sidecar_path(path)
    if xmp_sidecar.exists():
        entry["metadata_sidecars"] = [
            {
                "target": "immich_xmp",
                "path": xmp_sidecar.relative_to(archive_dir).as_posix(),
                "bytes": xmp_sidecar.stat().st_size,
            }
        ]
    return entry


def routing_manifest_file_entry(
    file_state: dict[str, Any],
    *,
    upload: dict[str, Any],
    groups: dict[str, dict[str, Any]],
    archive_dir: Path,
) -> dict[str, Any]:
    action = str(file_state.get("route_action") or "")
    group_name = upload_service.upload_file_resolved_group(file_state)
    entry: dict[str, Any] = {
        "source": {
            "path": str(file_state.get("path") or ""),
            "bytes": int(file_state.get("bytes") or 0),
        },
        "route": {
            "id": str(file_state.get("route_id") or ""),
            "action": action or ("upload" if group_name else ""),
        },
    }
    if file_state.get("sha256"):
        entry["source"]["sha256"] = str(file_state["sha256"])
    pair: dict[str, Any] = {}
    for source_key, output_key in (
        ("pair_kind", "kind"),
        ("pair_id", "id"),
        ("pair_role", "role"),
        ("pair_with", "with"),
    ):
        if file_state.get(source_key):
            pair[output_key] = str(file_state[source_key])
    if pair:
        entry["pair"] = pair
    matched_facts = file_state.get("route_matched_facts")
    if isinstance(matched_facts, dict) and matched_facts:
        entry["route"]["matched_facts"] = matched_facts
    if group_name:
        group_config = groups[group_name]
        group_rel = upload_service.upload_file_group_rel_for_state(
            file_state, group_name
        ).as_posix()
        entry["route"]["group"] = group_name
        entry["route"]["group_rel_path"] = group_rel
        if upload_service.upload_file_is_sidecar_evidence(file_state):
            entry["route"]["sidecar"] = {
                "id": str(file_state.get("sidecar_id") or ""),
                "format": str(file_state.get("sidecar_format") or "opaque"),
                "for": str(file_state.get("sidecar_for") or ""),
            }
            entry["output"] = {
                "kind": "none",
                "reason": "sidecar_evidence",
            }
            source_artifact = routing_manifest_sidecar_source_artifact_entry(
                file_state,
                upload=upload,
                groups=groups,
                archive_dir=archive_dir,
            )
            if source_artifact:
                entry["source_artifact"] = source_artifact
            return entry
        entry["output"] = routing_manifest_output_entry(
            archive_output_path_for_routed_file(
                file_state,
                group_name=group_name,
                group_config=group_config,
                archive_dir=archive_dir,
            ),
            archive_dir=archive_dir,
        )
    return entry


def routing_manifest_sidecar_source_artifact_entry(
    evidence_state: Mapping[str, Any],
    *,
    upload: dict[str, Any],
    groups: dict[str, dict[str, Any]],
    archive_dir: Path,
) -> dict[str, Any] | None:
    primary_source = str(evidence_state.get("sidecar_for") or "")
    evidence_dict = cast(dict[str, Any], evidence_state)
    group_name = upload_service.upload_file_resolved_group(evidence_dict)
    if not primary_source or not group_name:
        return None
    primary_state = next(
        (
            file_state
            for file_state in upload.get("files", [])
            if isinstance(file_state, dict) and str(file_state.get("path") or "") == primary_source
        ),
        None,
    )
    if primary_state is None:
        return None
    primary_group = upload_service.upload_file_resolved_group(primary_state)
    if not primary_group or primary_group not in groups:
        return None
    primary_group_config = groups[primary_group]
    if not group_produces_primary_archive_output(primary_group_config):
        return None
    primary_output = archive_output_path_for_routed_file(
        primary_state,
        group_name=primary_group,
        group_config=primary_group_config,
        archive_dir=archive_dir,
    )
    evidence_group_rel = upload_service.upload_file_group_rel_for_state(
        evidence_dict,
        group_name,
    )
    return {
        "kind": "source_artifact_sidecar",
        "primary_source": primary_source,
        "source_artifacts_path": source_artifact_sidecar_for_archive_output(primary_output)
        .relative_to(archive_dir)
        .as_posix(),
        "source_artifacts_entry": domain_models.normalize_posix(
            PurePosixPath("sidecars", evidence_group_rel.as_posix()).as_posix()
        ),
    }


def write_routing_manifest(
    job: dict[str, Any],
    upload: dict[str, Any],
    groups: dict[str, dict[str, Any]],
    archive_dir: Path,
) -> None:
    if not isinstance(job.get("routing"), dict):
        return
    manifest_path = archive_dir / runtime_config.ROUTING_MANIFEST_FILENAME
    created_at = str(job.get("routing_manifest_created_at") or "")
    if not created_at and manifest_path.is_file():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            existing = None
        if (
            isinstance(existing, dict)
            and existing.get("schema") == "munchy_api_client.routing-manifest"
            and existing.get("schema_version") == 1
            and existing.get("job_id") == str(job.get("job_id") or "")
            and existing.get("input_upload_id") == str(job.get("input_upload_id") or "")
            and existing.get("run_id") == str(job.get("run_id") or "")
            and isinstance(existing.get("created_at"), str)
            and existing["created_at"]
        ):
            created_at = str(existing["created_at"])
    if not created_at:
        created_at = utc_timestamp_now()
    job["routing_manifest_created_at"] = created_at
    files = [
        routing_manifest_file_entry(
            file_state,
            upload=upload,
            groups=groups,
            archive_dir=archive_dir,
        )
        for file_state in upload.get("files", [])
        if file_state.get("routed_at")
    ]
    payload = {
        "schema": "munchy_api_client.routing-manifest",
        "schema_version": 1,
        "created_at": created_at,
        "job_id": str(job.get("job_id") or ""),
        "input_upload_id": str(job.get("input_upload_id") or ""),
        **template_service.submission_template_summary(job),
        "run_id": str(job.get("run_id") or ""),
        "files": sorted(files, key=lambda item: str(item["source"]["path"])),
    }
    write_atomic_text(
        manifest_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )


def metadata_projection_config(group_config: dict[str, Any]) -> dict[str, Any]:
    raw = group_config.get("metadata_projection")
    if raw is False:
        return {
            "enabled": False,
            "target": "immich_xmp",
            "allow_missing_capture_date": False,
            "allow_missing_gps": False,
            "allow_missing_device_make": False,
            "allow_missing_device_model": False,
            "allow_missing_creators": False,
            "capture_date_sources": None,
            "gps_sources": None,
            "configured_gps": None,
            "device_make": None,
            "device_model": None,
            "creators": [],
            "tags": [],
            "include_context_tags": False,
        }
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise RuntimeError("metadata_projection must be a table")
    target = str(raw.get("target") or "immich_xmp")
    if target != "immich_xmp":
        raise RuntimeError(f"unsupported metadata projection target: {target}")
    tags = raw.get("tags") or []
    if not isinstance(tags, list):
        raise RuntimeError("metadata_projection.tags must be a list")
    capture_date_sources = raw.get("capture_date_sources")
    if capture_date_sources is not None and not isinstance(capture_date_sources, list):
        raise RuntimeError("metadata_projection.capture_date_sources must be a list")
    if isinstance(capture_date_sources, list):
        for source in capture_date_sources:
            if not isinstance(source, dict):
                raise RuntimeError(
                    "metadata_projection.capture_date_sources entries must be tables"
                )
    gps_sources = raw.get("gps_sources")
    if gps_sources is not None and not isinstance(gps_sources, list):
        raise RuntimeError("metadata_projection.gps_sources must be a list")
    if isinstance(gps_sources, list):
        for source in gps_sources:
            if not isinstance(source, dict):
                raise RuntimeError("metadata_projection.gps_sources entries must be tables")
    device = raw.get("device") or {}
    if not isinstance(device, dict):
        raise RuntimeError("metadata_projection.device must be a table")
    device_make = str(device.get("make") or "").strip() or None
    device_model = str(device.get("model") or "").strip() or None
    configured_gps = raw.get("gps")
    if configured_gps is not None and not isinstance(configured_gps, dict):
        raise RuntimeError("metadata_projection.gps must be a table")
    if "creator" in raw:
        raise RuntimeError("metadata_projection.creator is not supported; use creators = [...]")
    creators = raw.get("creators") or []
    if not isinstance(creators, list):
        raise RuntimeError("metadata_projection.creators must be a list")
    return {
        "enabled": bool(raw.get("enabled", True)),
        "target": target,
        "allow_missing_capture_date": bool(raw.get("allow_missing_capture_date", False)),
        "allow_missing_gps": bool(raw.get("allow_missing_gps", False)),
        "allow_missing_device_make": bool(raw.get("allow_missing_device_make", False)),
        "allow_missing_device_model": bool(raw.get("allow_missing_device_model", False)),
        "allow_missing_creators": bool(raw.get("allow_missing_creators", False)),
        "capture_date_sources": copy.deepcopy(capture_date_sources),
        "gps_sources": copy.deepcopy(gps_sources),
        "configured_gps": copy.deepcopy(configured_gps),
        "device_make": device_make,
        "device_model": device_model,
        "creators": [str(creator).strip() for creator in creators if str(creator).strip()],
        "tags": [str(tag).strip() for tag in tags if str(tag).strip()],
        "include_context_tags": bool(raw.get("include_context_tags", True)),
    }


def metadata_projection_enabled(group_config: dict[str, Any]) -> bool:
    return bool(metadata_projection_config(group_config)["enabled"])


def metadata_projection_facts_for_path(
    rel_path: str,
    path: Path,
    *,
    provenance: Mapping[str, Any] | None = None,
    sidecar_facts: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    probe_summary: dict[str, Any] | None = None
    try:
        probe_summary = routing_probe_summary(ffprobe_for_routing(path))
    except domain_errors.RoutingFailed as exc:
        log.debug("ffprobe metadata projection summary skipped for %s: %s", rel_path, exc)
    exiftool_summary: dict[str, Any] | None = None
    try:
        exiftool_summary = routing_exiftool_summary(exiftool_for_routing(path))
    except domain_errors.RoutingFailed as exc:
        log.debug("exiftool metadata projection summary skipped for %s: %s", rel_path, exc)
    facts = routing_file_facts(
        rel_path,
        probe_summary=probe_summary,
        exiftool_summary=exiftool_summary,
    )
    if provenance:
        facts["provenance"] = dict(provenance)
    if sidecar_facts:
        ids: list[str] = []
        for sidecar_id, payload in sorted(sidecar_facts.items()):
            if not isinstance(payload, Mapping):
                continue
            sidecar_key = str(sidecar_id).strip()
            if not sidecar_key:
                continue
            ids.append(sidecar_key)
            facts[f"sidecars.{sidecar_key}.path"] = str(payload.get("path") or "")
            facts[f"sidecars.{sidecar_key}.format"] = str(payload.get("format") or "")
            nested = payload.get("facts")
            if isinstance(nested, Mapping):
                for key, value in nested.items():
                    facts[f"sidecars.{sidecar_key}.facts.{key}"] = value
        if ids:
            facts["sidecars.ids"] = ids
    return facts


def projection_metadata_from_source(
    rel_path: str,
    source_path: Path,
    *,
    group_config: dict[str, Any],
    provenance: Mapping[str, Any] | None = None,
    sidecar_facts: Mapping[str, Mapping[str, Any]] | None = None,
    tags: list[str] | None = None,
) -> ProjectionMetadata:
    config = metadata_projection_config(group_config)
    try:
        return project_immich_metadata(
            metadata_projection_facts_for_path(
                rel_path,
                source_path,
                provenance=provenance,
                sidecar_facts=sidecar_facts,
            ),
            allow_missing_capture_date=bool(config["allow_missing_capture_date"]),
            allow_missing_gps=bool(config["allow_missing_gps"]),
            allow_missing_device_make=bool(config["allow_missing_device_make"]),
            allow_missing_device_model=bool(config["allow_missing_device_model"]),
            allow_missing_creators=bool(config["allow_missing_creators"]),
            capture_date_sources=cast(
                list[dict[str, Any]] | None,
                config.get("capture_date_sources"),
            ),
            gps_sources=cast(
                list[dict[str, Any]] | None,
                config.get("gps_sources"),
            ),
            configured_gps=cast(dict[str, Any] | None, config.get("configured_gps")),
            device_make=cast(str | None, config.get("device_make")),
            device_model=cast(str | None, config.get("device_model")),
            creators=cast(list[str], config.get("creators")),
            tags=tags if tags is not None else cast(list[str], config["tags"]),
        )
    except MetadataProjectionError as exc:
        raise RuntimeError(f"metadata projection failed for {rel_path}: {exc}") from exc


def _provenance_state_facts(state: Mapping[str, Any]) -> dict[str, Any]:
    filesystem = state.get("filesystem_metadata")
    timestamps: dict[str, object] = {}
    if isinstance(filesystem, Mapping):
        for item in filesystem.get("timestamps", []):
            if isinstance(item, Mapping) and item.get("kind") and item.get("value"):
                timestamps[str(item["kind"])] = item["value"]
    return {"state_id": str(state.get("id") or ""), "timestamps": timestamps}


def file_state_provenance_facts(file_state: Mapping[str, Any]) -> dict[str, Any]:
    provenance = file_state.get("provenance")
    if not isinstance(provenance, Mapping):
        raise RuntimeError("Munchy input file has no provenance accounting")
    status = str(provenance.get("status") or "")
    if status == "omitted":
        return {
            "status": "omitted",
            "omission_reason": str(provenance.get("omission_reason") or ""),
        }
    journal_id = str(provenance.get("journal_id") or "")
    upload_id = str(file_state.get("input_upload_id") or "")
    content = upload_service.input_provenance_journal_path(upload_id, journal_id).read_bytes()
    summary = validate_journal(content)
    states = [
        state
        for frame in parse_journal(content)
        for state in (
            frame.document.get("body", {}).get("assertions", {}).get("states", [])
            if isinstance(frame.document.get("body"), Mapping)
            else []
        )
        if isinstance(state, Mapping) and state.get("lineage_id") == summary.primary_lineage_id
    ]
    if not states:
        raise RuntimeError("Munchy input provenance has no primary-lineage states")
    current = next(
        (state for state in reversed(states) if state.get("id") == summary.current_state_id),
        None,
    )
    if current is None:
        raise RuntimeError("Munchy input provenance has no current state")
    return {
        "status": "captured",
        "journal_id": summary.journal_id,
        "origin": _provenance_state_facts(states[0]),
        "current": _provenance_state_facts(current),
    }


def metadata_projection_sidecar_facts(
    upload: dict[str, Any],
    file_state: Mapping[str, Any],
    *,
    routing: Mapping[str, Any] | None = None,
    source_paths_by_path: Mapping[str, Path] | None = None,
) -> dict[str, dict[str, Any]]:
    sidecars: dict[str, dict[str, Any]] = {}
    for evidence in upload_service.sidecar_evidence_files_for_primary(upload, file_state):
        sidecar_id = str(evidence.get("sidecar_id") or "").strip()
        sidecar_format = str(evidence.get("sidecar_format") or "opaque").strip()
        if not sidecar_id:
            continue
        tags = metadata_projection_sidecar_exiftool_tags(routing, sidecar_id=sidecar_id)
        if not tags:
            continue
        fact_extractors = metadata_projection_sidecar_fact_extractors(
            routing,
            sidecar_id=sidecar_id,
        )
        rel_path = str(evidence.get("path") or "")
        source_path = (
            source_paths_by_path.get(rel_path) if source_paths_by_path is not None else None
        )
        if source_path is None:
            source_path = upload_file_data_path(evidence)
        exiftool_summary = routing_exiftool_summary(exiftool_for_routing(source_path, tags=tags))
        sidecars[sidecar_id] = {
            "path": rel_path,
            "format": sidecar_format,
            "facts": routing_file_facts(
                rel_path,
                exiftool_summary=exiftool_summary,
                exiftool_fact_extractors=fact_extractors,
            ),
        }
    return sidecars


def job_routing(job: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not isinstance(job, Mapping):
        return None
    routing = job.get("routing")
    return cast(Mapping[str, Any], routing) if isinstance(routing, Mapping) else None


def metadata_projection_sidecar_exiftool_tags(
    routing: Mapping[str, Any] | None,
    *,
    sidecar_id: str,
) -> tuple[str, ...]:
    if not isinstance(routing, Mapping):
        return ()
    for rule in sidecar_rules(routing):
        if str(rule.get("id") or "").strip() == sidecar_id:
            return sidecar_rule_exiftool_tags(rule)
    return ()


def metadata_projection_sidecar_fact_extractors(
    routing: Mapping[str, Any] | None,
    *,
    sidecar_id: str,
) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(routing, Mapping):
        return ()
    for rule in sidecar_rules(routing):
        if str(rule.get("id") or "").strip() == sidecar_id:
            return sidecar_rule_fact_extractors(rule)
    return ()


def xmp_evidence_sidecar_path(
    upload: dict[str, Any],
    file_state: Mapping[str, Any],
) -> Path | None:
    for evidence in upload_service.sidecar_evidence_files_for_primary(upload, file_state):
        if str(evidence.get("sidecar_format") or "").casefold() != "xmp":
            continue
        return upload_file_data_path(evidence)
    return None


def metadata_projection_with_tags(
    metadata: ProjectionMetadata,
    tags: list[str],
) -> ProjectionMetadata:
    return ProjectionMetadata(
        capture_date=metadata.capture_date,
        capture_date_source=metadata.capture_date_source,
        gps=metadata.gps,
        gps_source=metadata.gps_source,
        device_make=metadata.device_make,
        device_model=metadata.device_model,
        creators=metadata.creators,
        tags=tuple(tags),
    )


def projection_metadata_satisfies_config(
    metadata: ProjectionMetadata,
    config: dict[str, Any],
) -> bool:
    if not config["allow_missing_capture_date"] and not metadata.capture_date:
        return False
    if not config["allow_missing_gps"] and metadata.gps is None:
        return False
    expected_make = cast(str | None, config.get("device_make"))
    if expected_make and metadata.device_make != expected_make:
        return False
    if not expected_make and not config["allow_missing_device_make"] and not metadata.device_make:
        return False
    expected_model = cast(str | None, config.get("device_model"))
    if expected_model and metadata.device_model != expected_model:
        return False
    if (
        not expected_model
        and not config["allow_missing_device_model"]
        and not metadata.device_model
    ):
        return False
    expected_creators = tuple(cast(list[str], config.get("creators") or []))
    if expected_creators and metadata.creators != expected_creators:
        return False
    if not expected_creators and not config["allow_missing_creators"] and not metadata.creators:
        return False
    return True


def container_metadata_for_gpu_payload(
    job: dict[str, Any],
    upload: dict[str, Any],
    file_states: list[dict[str, Any]],
    *,
    group_name: str,
    group_config: dict[str, Any],
    tasks: Sequence[str],
    source_paths_by_path: Mapping[str, Path] | None = None,
) -> tuple[dict[str, dict[str, Any]], bool]:
    if not gpu_tasks_require_container_metadata(tasks, group_config):
        return {}, False
    metadata_by_rel_path: dict[str, dict[str, Any]] = {}
    changed = False
    for file_state in file_states:
        if ensure_file_projection_metadata(
            upload,
            file_state,
            job=job,
            group_config=group_config,
            source_path=source_paths_by_path.get(str(file_state["path"]))
            if source_paths_by_path is not None
            else None,
            sidecar_source_paths_by_path=source_paths_by_path,
        ):
            changed = True
        stored = file_state.get("metadata_projection_metadata")
        if isinstance(stored, dict):
            rel_path = upload_service.upload_file_group_rel_for_state(
                file_state, group_name
            ).as_posix()
            metadata_by_rel_path[rel_path] = copy.deepcopy(stored)
    return metadata_by_rel_path, changed


def gpu_tasks_require_container_metadata(
    tasks: Sequence[str],
    group_config: dict[str, Any],
) -> bool:
    return "archive_video" in {str(task) for task in tasks} and metadata_projection_enabled(
        group_config
    )


def ensure_file_projection_metadata(
    upload: dict[str, Any],
    file_state: dict[str, Any],
    *,
    job: Mapping[str, Any] | None = None,
    group_config: dict[str, Any],
    source_path: Path | None = None,
    sidecar_source_paths_by_path: Mapping[str, Path] | None = None,
) -> bool:
    if not metadata_projection_enabled(group_config):
        return False
    config = metadata_projection_config(group_config)
    stored = file_state.get("metadata_projection_metadata")
    if isinstance(stored, dict) and projection_metadata_satisfies_config(
        ProjectionMetadata.from_dict(stored),
        config,
    ):
        return False
    metadata = projection_metadata_from_source(
        str(file_state["path"]),
        source_path if source_path is not None else upload_file_data_path(file_state),
        group_config=group_config,
        provenance=file_state_provenance_facts(file_state),
        sidecar_facts=metadata_projection_sidecar_facts(
            upload,
            file_state,
            routing=job_routing(job),
            source_paths_by_path=sidecar_source_paths_by_path,
        ),
    )
    file_state["metadata_projection_metadata"] = metadata.as_dict()
    file_state["metadata_projection_captured_at"] = utc_timestamp_now()
    return True


def projection_metadata_for_file_output(
    upload: dict[str, Any],
    file_state: dict[str, Any],
    *,
    job: dict[str, Any],
    group_name: str,
    group_config: dict[str, Any],
    output_path: Path,
) -> ProjectionMetadata:
    tags = metadata_projection_tags_for_file(
        job,
        file_state,
        group_name=group_name,
        group_config=group_config,
    )
    stored = file_state.get("metadata_projection_metadata")
    if isinstance(stored, dict):
        return metadata_projection_with_tags(
            ProjectionMetadata.from_dict(stored),
            tags,
        )
    source_path: Path | None
    try:
        source_path = upload_file_data_path(file_state)
    except domain_errors.RoutingFailed:
        source_path = output_path if output_path.exists() else None
    if source_path is None:
        raise RuntimeError(
            f"metadata projection cannot locate source metadata for {file_state.get('path')}"
        )
    return projection_metadata_from_source(
        str(file_state["path"]),
        source_path,
        group_config=group_config,
        provenance=file_state_provenance_facts(file_state),
        sidecar_facts=metadata_projection_sidecar_facts(
            upload,
            file_state,
            routing=job_routing(job),
        ),
        tags=tags,
    )


def metadata_projection_tags_for_file(
    job: dict[str, Any],
    file_state: dict[str, Any],
    *,
    group_name: str,
    group_config: dict[str, Any],
) -> list[str]:
    config = metadata_projection_config(group_config)
    tags = list(cast(list[str], config["tags"]))
    if not config["include_context_tags"]:
        return dedup_metadata_projection_tags(tags)

    template_id = str(job.get("template_id") or "").strip()
    if template_id:
        tags.append(f"munchy/template/{template_id}")
    if group_name:
        tags.append(f"munchy/group/{group_name}")
    route_id = str(file_state.get("route_id") or "").strip()
    if route_id:
        tags.append(f"munchy/route/{route_id}")
    group_rel = str(file_state.get("resolved_group_rel") or "").strip()
    if group_rel:
        parent = Path(domain_models.normalize_posix(group_rel)).parent.as_posix()
        if parent and parent != ".":
            tags.append(f"munchy/output/{parent}")
    pair_kind = str(file_state.get("pair_kind") or "").strip()
    pair_role = str(file_state.get("pair_role") or "").strip()
    if pair_kind:
        tags.append(f"munchy/pair/{pair_kind}")
    if pair_kind and pair_role:
        tags.append(f"munchy/pair/{pair_kind}/{pair_role}")
    return dedup_metadata_projection_tags(tags)


def dedup_metadata_projection_tags(tags: list[str]) -> list[str]:
    return list(dict.fromkeys(tag.strip() for tag in tags if tag.strip()))


def write_atomic_text(path: Path, text: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        return True
    finally:
        tmp.unlink(missing_ok=True)


def metadata_projection_handed_off_paths(job: dict[str, Any]) -> set[str]:
    adapter = handoff_service.optional_handoff_adapter(job)
    if adapter is None:
        return set()
    paths = adapter.handed_off_paths(job)
    job_id = str(job.get("job_id") or "")
    if not job_id:
        return paths
    current = state_store.read_state("job", job_id)
    if not isinstance(current, dict):
        return paths
    paths.update(adapter.handed_off_paths(current))
    current_adapter_state = current.get("handoff_adapter_state")
    incoming_adapter_state = job.get("handoff_adapter_state")
    if isinstance(current_adapter_state, dict) and isinstance(incoming_adapter_state, dict):
        job["handoff_adapter_state"] = adapter.merge_state(
            current_adapter_state,
            incoming_adapter_state,
        )
    elif isinstance(current_adapter_state, dict):
        job["handoff_adapter_state"] = current_adapter_state
    return paths


def write_metadata_projection_sidecars(
    job: dict[str, Any],
    upload: dict[str, Any],
    groups: dict[str, dict[str, Any]],
    archive_dir: Path,
) -> dict[str, Any]:
    upload_id = str(upload["input_upload_id"])
    with execution_runtime.input_upload_state_lock(upload_id):
        return write_metadata_projection_sidecars_locked(
            job,
            upload_service.load_input_upload_raw(upload_id),
            groups,
            archive_dir,
        )


def write_metadata_projection_sidecars_locked(
    job: dict[str, Any],
    upload: dict[str, Any],
    groups: dict[str, dict[str, Any]],
    archive_dir: Path,
) -> dict[str, Any]:
    sidecars_written = 0
    groups_written: dict[str, int] = {}
    upload_changed = False
    handed_off_paths = metadata_projection_handed_off_paths(job)
    adapter = handoff_service.optional_handoff_adapter(job)
    for group_name, group_config in sorted(groups.items()):
        config = metadata_projection_config(group_config)
        if not config["enabled"]:
            continue
        if not group_produces_primary_archive_output(group_config):
            continue
        for file_state in upload_service.mutable_primary_upload_files_for_groups(
            upload, {group_name}
        ):
            output = archive_output_path_for_routed_file(
                file_state,
                group_name=group_name,
                group_config=group_config,
                archive_dir=archive_dir,
            )
            if not output.exists():
                output_rel = path_relative_to_archive(output, archive_dir)
                if output_rel not in handed_off_paths:
                    raise RuntimeError(
                        "metadata projection output is missing for "
                        f"{file_state.get('path')}: {output}"
                    )
            if ensure_file_projection_metadata(
                upload,
                file_state,
                job=job,
                group_config=group_config,
            ):
                upload_changed = True
            metadata = projection_metadata_for_file_output(
                upload,
                file_state,
                job=job,
                group_name=group_name,
                group_config=group_config,
                output_path=output,
            )
            sidecar = immich_xmp_sidecar_path(output)
            sidecar_rel = sidecar.relative_to(archive_dir).as_posix()
            registered = adapter.artifact_record(job, sidecar_rel) if adapter is not None else None
            if isinstance(registered, dict):
                assert adapter is not None
                groups_written[group_name] = groups_written.get(group_name, 0) + 1
                if file_state.get("metadata_projection_sidecar") != sidecar_rel:
                    file_state["metadata_projection_sidecar"] = sidecar_rel
                    upload_changed = True
                if adapter.artifact_complete(registered):
                    continue
                if not sidecar.is_file():
                    raise RuntimeError(
                        f"registered metadata projection sidecar is missing: {sidecar_rel}"
                    )
                if str(registered.get("sha256") or "") != upload_service.file_sha256(sidecar):
                    raise RuntimeError(
                        f"registered metadata projection sidecar changed: {sidecar_rel}"
                    )
                continue
            metadata_date = str(file_state.get("metadata_projection_captured_at") or "")
            if not metadata_date:
                metadata_date = utc_timestamp_now()
                file_state["metadata_projection_captured_at"] = metadata_date
                upload_changed = True
            xmp_evidence = (
                xmp_evidence_sidecar_path(upload, file_state)
                if domain_models.normalize_output_mode(
                    str(group_config.get("output_mode") or "video")
                )
                == "preserve"
                else None
            )
            if xmp_evidence is not None:
                try:
                    rendered = merge_immich_xmp_sidecar(
                        xmp_evidence.read_text(encoding="utf-8"),
                        metadata,
                        metadata_date=metadata_date,
                    )
                except MetadataProjectionError as exc:
                    raise RuntimeError(
                        f"metadata projection failed for {file_state.get('path')}: {exc}"
                    ) from exc
            else:
                rendered = render_immich_xmp_sidecar(metadata, metadata_date=metadata_date)
            if write_atomic_text(sidecar, rendered):
                sidecars_written += 1
            groups_written[group_name] = groups_written.get(group_name, 0) + 1
            if file_state.get("metadata_projection_sidecar") != sidecar_rel:
                file_state["metadata_projection_sidecar"] = sidecar_rel
                upload_changed = True
    job["metadata_projection_result"] = {
        "updated_at": utc_timestamp_now(),
        "target": "immich_xmp",
        "sidecars": sum(groups_written.values()),
        "sidecars_written": sidecars_written,
        "groups": groups_written,
    }
    if upload_changed:
        upload = upload_service.save_input_upload_raw(upload)
    state_store.save_job(job)
    return upload


def source_artifact_sidecar_for_archive_output(output: Path) -> Path:
    return Path(f"{output}.source-artifacts.tar.zst")


def archive_dir_artifact_paths(archive_dir: Path) -> list[Path]:
    if not archive_dir.is_dir():
        return []
    return sorted(path for path in archive_dir.rglob("*") if path.is_file())


def path_relative_to_archive(path: Path, archive_dir: Path) -> str | None:
    try:
        return path.relative_to(archive_dir).as_posix()
    except ValueError:
        return None


def primary_archive_output_paths(job: dict[str, Any], archive_dir: Path) -> list[str]:
    eager = job.get("eager_archive")
    if not isinstance(eager, dict):
        return []
    files = eager.get("files")
    if not isinstance(files, dict):
        return []
    paths: list[str] = []
    seen: set[str] = set()
    for item in files.values():
        if not isinstance(item, dict) or item.get("state") != "encoded":
            continue
        output_value = item.get("output")
        if not output_value:
            continue
        rel_path = path_relative_to_archive(Path(str(output_value)), archive_dir)
        if rel_path is None or rel_path in seen:
            continue
        seen.add(rel_path)
        paths.append(rel_path)
    return paths

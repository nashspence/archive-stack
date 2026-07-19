from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from munchy_api_client.local_files import LocalFileCandidate
from munchy_api_client.routing import (
    RoutingFile,
    apply_sidecar_rules,
    exiftool_routing_facts,
    routing_exiftool_summary,
    routing_exiftool_tags,
    routing_file_facts,
    routing_file_requires_exiftool,
    routing_file_requires_probe,
    routing_probe_summary,
    sidecar_exiftool_fact_requests,
)


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
        raise RuntimeError(f"ffprobe failed for {path}: {detail}")
    payload = json.loads(proc.stdout or "{}")
    if not isinstance(payload, dict):
        raise RuntimeError(f"ffprobe returned non-object JSON for {path}")
    return payload


def exiftool_for_routing(path: Path, *, tags: Sequence[str]) -> dict[str, Any]:
    proc = subprocess.run(
        [
            "exiftool",
            "-j",
            "-a",
            "-G1:4",
            "-s",
            "-ee",
            *[f"-{tag}" for tag in tags],
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


def routing_plan_files(
    candidates: Sequence[LocalFileCandidate],
    *,
    routing: Mapping[str, Any],
) -> list[RoutingFile]:
    exiftool_tags = routing_exiftool_tags(routing)
    path_facts_by_path = {item.rel_path: routing_file_facts(item.rel_path) for item in candidates}
    sidecar_fact_requests = sidecar_exiftool_fact_requests(routing, path_facts_by_path)
    sidecar_facts_by_path: dict[str, dict[str, Any]] = {}
    sidecar_facts_errors_by_path: dict[str, str] = {}
    for item in candidates:
        sidecar_request = sidecar_fact_requests.get(item.rel_path)
        if sidecar_request is None or not sidecar_request.tags:
            continue
        try:
            sidecar_facts_by_path[item.rel_path] = exiftool_routing_facts(
                routing_exiftool_summary(
                    exiftool_for_routing(item.source, tags=sidecar_request.tags)
                ),
                fact_extractors=sidecar_request.fact_extractors,
            )
        except Exception as exc:
            sidecar_facts_errors_by_path[item.rel_path] = str(exc)[:1000]
    base_facts_by_path = apply_sidecar_rules(
        routing,
        path_facts_by_path,
        sidecar_facts_by_path=sidecar_facts_by_path,
        sidecar_facts_errors_by_path=sidecar_facts_errors_by_path,
        require_configured_facts=False,
    )
    files: list[RoutingFile] = []
    for item in candidates:
        base_facts = base_facts_by_path.get(item.rel_path) or path_facts_by_path[item.rel_path]
        is_sidecar_evidence = base_facts.get("sidecar.role") == "evidence"
        probe_summary: dict[str, Any] | None = None
        probe_error: str | None = None
        if not is_sidecar_evidence and routing_file_requires_probe(
            routing,
            routing_file_facts(item.rel_path, routing_facts=base_facts),
        ):
            try:
                probe_summary = routing_probe_summary(ffprobe_for_routing(item.source))
            except Exception as exc:
                probe_error = str(exc)[:1000]
        exiftool_summary: dict[str, Any] | None = None
        facts_error: str | None = None
        probe_facts = routing_file_facts(
            item.rel_path,
            probe_summary=probe_summary,
            routing_facts=base_facts,
        )
        if not is_sidecar_evidence and routing_file_requires_exiftool(
            routing,
            probe_facts,
        ):
            try:
                exiftool_summary = routing_exiftool_summary(
                    exiftool_for_routing(item.source, tags=exiftool_tags)
                )
            except Exception as exc:
                facts_error = str(exc)[:1000]
        files.append(
            RoutingFile(
                path=item.rel_path,
                bytes=item.bytes,
                probe_summary=probe_summary,
                probe_error=probe_error,
                routing_facts=routing_file_facts(
                    item.rel_path,
                    probe_summary=probe_summary,
                    exiftool_summary=exiftool_summary,
                    routing_facts=base_facts,
                ),
                facts_error=facts_error,
                sidecar_facts=sidecar_facts_by_path.get(item.rel_path),
                sidecar_facts_error=sidecar_facts_errors_by_path.get(item.rel_path),
            )
        )
    return files

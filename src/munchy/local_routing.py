from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from munchy.local_files import LocalFileCandidate
from munchy.profile_routing import (
    ProfileRoutingFile,
    exiftool_routing_facts,
    profile_routing_exiftool_tags,
    profile_routing_file_requires_exiftool,
    profile_routing_file_requires_probe,
    routing_exiftool_summary,
    routing_file_facts,
    routing_probe_summary,
    sidecar_exiftool_tag_requests,
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
            "-G1",
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
    profile_routing: Mapping[str, Any],
) -> list[ProfileRoutingFile]:
    exiftool_tags = profile_routing_exiftool_tags(profile_routing)
    path_facts_by_path = {item.rel_path: routing_file_facts(item.rel_path) for item in candidates}
    sidecar_tag_requests = sidecar_exiftool_tag_requests(profile_routing, path_facts_by_path)
    files: list[ProfileRoutingFile] = []
    for item in candidates:
        probe_summary: dict[str, Any] | None = None
        probe_error: str | None = None
        if profile_routing_file_requires_probe(
            profile_routing,
            path_facts_by_path[item.rel_path],
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
        )
        if profile_routing_file_requires_exiftool(profile_routing, probe_facts):
            try:
                exiftool_summary = routing_exiftool_summary(
                    exiftool_for_routing(item.source, tags=exiftool_tags)
                )
            except Exception as exc:
                facts_error = str(exc)[:1000]
        sidecar_facts: dict[str, Any] | None = None
        sidecar_facts_error: str | None = None
        sidecar_tags = sidecar_tag_requests.get(item.rel_path)
        if sidecar_tags:
            try:
                sidecar_facts = exiftool_routing_facts(
                    routing_exiftool_summary(exiftool_for_routing(item.source, tags=sidecar_tags))
                )
            except Exception as exc:
                sidecar_facts_error = str(exc)[:1000]
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
                sidecar_facts=sidecar_facts,
                sidecar_facts_error=sidecar_facts_error,
            )
        )
    return files

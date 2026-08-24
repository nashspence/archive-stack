"""Derive standalone runtime attribution requirements from executable inputs."""

from __future__ import annotations

import re
import shlex
import tomllib
from pathlib import Path
from typing import Any


class RuntimeAttributionError(ValueError):
    """Raised when runtime payload and lock authority disagree."""


def _tool_name(value: str) -> str:
    return value.rpartition(":")[2]


def _configured_version(value: object, *, tool: str) -> str:
    version: object
    if isinstance(value, str):
        version = value
    elif isinstance(value, dict):
        version = value.get("version")
    else:
        version = None
    if not isinstance(version, str) or not version:
        raise RuntimeAttributionError(f"mise tool has no exact configured version: {tool}")
    return version


def locked_tool_versions(root: Path) -> dict[str, str]:
    """Return canonical tool names whose mise config and lock versions agree."""

    configured_document = tomllib.loads((root / "mise.toml").read_text(encoding="utf-8"))
    lock_document = tomllib.loads((root / "mise.lock").read_text(encoding="utf-8"))
    configured = configured_document.get("tools")
    locked = lock_document.get("tools")
    if not isinstance(configured, dict) or not isinstance(locked, dict):
        raise RuntimeAttributionError("mise config and lock must contain tool tables")

    result: dict[str, str] = {}
    for configured_name, value in configured.items():
        name = _tool_name(str(configured_name))
        version = _configured_version(value, tool=str(configured_name))
        if name in result:
            raise RuntimeAttributionError(f"mise config repeats canonical tool name: {name}")
        entries = locked.get(configured_name)
        if not isinstance(entries, list) or len(entries) != 1:
            raise RuntimeAttributionError(
                f"mise lock does not contain one exact entry for {configured_name}"
            )
        locked_version = entries[0].get("version") if isinstance(entries[0], dict) else None
        if locked_version != version:
            raise RuntimeAttributionError(
                f"mise config and lock versions differ for {configured_name}: "
                f"{version!r} != {locked_version!r}"
            )
        result[name] = version
    return result


def _final_stage(dockerfile: Path) -> str:
    text = dockerfile.read_text(encoding="utf-8")
    starts = [match.start() for match in re.finditer(r"(?m)^FROM\s+", text)]
    if not starts:
        raise RuntimeAttributionError(f"Dockerfile has no build stage: {dockerfile}")
    return text[starts[-1] :]


def locked_runtime_payloads(root: Path, dockerfile: Path) -> dict[str, str]:
    """Derive mise-managed tools copied into a Dockerfile's final stage."""

    payload_names: set[str] = set()
    prefix = "/opt/riverhog-tools/"
    for raw_line in _final_stage(dockerfile).splitlines():
        line = raw_line.strip()
        if not line.startswith("COPY "):
            continue
        words = shlex.split(line)
        if "--from=locked-tools" not in words:
            continue
        operands = [word for word in words[1:] if not word.startswith("--")]
        if len(operands) != 2:
            raise RuntimeAttributionError(
                f"locked-tools COPY must have one source and destination: {line}"
            )
        source = operands[0]
        if not source.startswith(prefix):
            continue
        relative = source.removeprefix(prefix).strip("/")
        parts = relative.split("/") if relative else []
        if not parts or parts[0] == "licenses":
            continue
        if parts[0] == "bin":
            if len(parts) != 2:
                raise RuntimeAttributionError(
                    f"final runtime binary COPY is not an exact tool: {source}"
                )
            name = parts[1]
        else:
            name = parts[0]
        payload_names.add(name)

    versions = locked_tool_versions(root)
    missing = payload_names - versions.keys()
    if missing:
        raise RuntimeAttributionError(
            f"final runtime payloads have no exact mise lock: {sorted(missing)}"
        )
    return {name: versions[name] for name in sorted(payload_names)}


def checked_attribution_sources(root: Path) -> list[dict[str, Any]]:
    """Load the exceptional attribution texts checked into ``third_party``."""

    sources: list[dict[str, Any]] = []
    for path in sorted((root / "third_party").glob("*/*/SOURCE.toml")):
        document = tomllib.loads(path.read_text(encoding="utf-8"))
        sources.append({**document, "metadata_path": path})
    return sources

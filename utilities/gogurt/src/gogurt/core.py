from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from os import PathLike
from pathlib import Path
from typing import Any

from config_validation import ConfigError, load_yaml_config, validate_json_schema

DEFAULT_GOGURT_MARKER_NAME = ".gogurt"
DEFAULT_GOGURT_CONFIG_FILENAME = "gogurt-routes.yaml"
MAX_GOGURT_MARKER_BYTES = 4096
GOGURT_EMOJI = "🛹"
_COMMAND_PLACEHOLDERS = {"{config_dir}", "{mount_point}", "{python}"}
_PLACEHOLDER_RE = re.compile(r"\{[^{}]+\}")

GOGURT_ROUTES_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "schema_version": {"type": "integer", "const": 1},
        "kind": {"type": "string", "const": "gogurt.routes"},
        "routes": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "enabled": {"type": "boolean"},
                    "command": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["schema_version", "kind", "routes"],
    "additionalProperties": False,
}

PathInput = str | PathLike[str]


@dataclass(frozen=True, slots=True)
class GogurtAction:
    route: str
    command: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GogurtMarker:
    route: str
    identity: str


def _marker_identity(info: os.stat_result, content: bytes) -> str:
    payload = "\0".join(
        (
            str(info.st_dev),
            str(info.st_ino),
            str(info.st_size),
            str(info.st_mtime_ns),
            sha256(content).hexdigest(),
        )
    )
    return sha256(payload.encode("ascii")).hexdigest()


def read_gogurt_marker(marker: PathInput) -> GogurtMarker:
    path = Path(marker)
    try:
        initial = path.lstat()
    except FileNotFoundError:
        raise
    if not stat.S_ISREG(initial.st_mode) or path.is_symlink():
        raise ConfigError(f"gogurt marker must be a regular file: {path}")
    if initial.st_size > MAX_GOGURT_MARKER_BYTES:
        raise ConfigError(f"gogurt marker exceeds {MAX_GOGURT_MARKER_BYTES} bytes: {path}")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ConfigError(f"gogurt marker must be a regular file: {path}")
        if opened.st_size > MAX_GOGURT_MARKER_BYTES:
            raise ConfigError(f"gogurt marker exceeds {MAX_GOGURT_MARKER_BYTES} bytes: {path}")
        content = os.read(descriptor, MAX_GOGURT_MARKER_BYTES + 1)
        final = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if len(content) > MAX_GOGURT_MARKER_BYTES:
        raise ConfigError(f"gogurt marker exceeds {MAX_GOGURT_MARKER_BYTES} bytes: {path}")
    if (
        initial.st_dev,
        initial.st_ino,
        initial.st_size,
        initial.st_mtime_ns,
    ) != (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_mtime_ns,
    ) or (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_mtime_ns,
    ) != (
        final.st_dev,
        final.st_ino,
        final.st_size,
        final.st_mtime_ns,
    ):
        raise ConfigError(f"gogurt marker changed while it was read: {path}")

    try:
        marker_text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"gogurt marker must be strict UTF-8: {path}") from exc
    marker_lines = marker_text.splitlines()
    if len(marker_lines) != 1 or not marker_lines[0].strip():
        raise ConfigError(f"gogurt marker must contain one route name: {path}")
    route = marker_lines[0].strip()
    if route != marker_lines[0]:
        raise ConfigError(f"gogurt marker route may not have surrounding whitespace: {path}")
    return GogurtMarker(route=route, identity=_marker_identity(final, content))


def default_gogurt_config_file(
    config_dir: PathInput,
    *,
    filename: str = DEFAULT_GOGURT_CONFIG_FILENAME,
) -> Path:
    return Path(config_dir).expanduser().resolve().parent / filename


def _validate_gogurt_route(route_name: str) -> None:
    if not route_name or "/" in route_name or "\\" in route_name or route_name in {".", ".."}:
        raise ConfigError(f"invalid gogurt route: {route_name!r}")


def _validate_gogurt_marker_name(marker_name: str) -> None:
    if not marker_name or "/" in marker_name or "\\" in marker_name or marker_name in {".", ".."}:
        raise ConfigError(f"invalid gogurt marker name: {marker_name!r}")


def _route_command(route_name: str, route: Mapping[str, Any]) -> tuple[str, ...]:
    raw_command = route.get("command")
    if not isinstance(raw_command, list) or not raw_command:
        raise ConfigError(f"gogurt route {route_name!r}.command must be a nonempty list")
    command = tuple(str(token) for token in raw_command)
    if any(not token.strip() for token in command):
        raise ConfigError(f"gogurt route {route_name!r}.command contains an empty token")
    if command.count("{mount_point}") != 1:
        raise ConfigError(
            f"gogurt route {route_name!r}.command must contain one {{mount_point}} token"
        )
    if "{python}" in command[1:]:
        raise ConfigError(
            f"gogurt route {route_name!r}.command may use {{python}} only as its executable"
        )
    unknown = sorted(
        {
            placeholder
            for token in command
            for placeholder in _PLACEHOLDER_RE.findall(token)
            if placeholder not in _COMMAND_PLACEHOLDERS
        }
    )
    if unknown:
        raise ConfigError(
            f"gogurt route {route_name!r}.command has unknown placeholders: {', '.join(unknown)}"
        )
    for token in command:
        if "{mount_point}" in token and token != "{mount_point}":
            raise ConfigError(
                f"gogurt route {route_name!r}.command must use {{mount_point}} as one token"
            )
        if "{python}" in token and token != "{python}":
            raise ConfigError(
                f"gogurt route {route_name!r}.command must use {{python}} as one token"
            )
    return command


def load_gogurt_actions(config_file: PathInput) -> list[GogurtAction]:
    path = Path(config_file).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"gogurt route config not found: {path}")

    raw = load_yaml_config(path)
    validate_json_schema(raw, GOGURT_ROUTES_SCHEMA, label=str(path))
    routes = raw.get("routes")
    if not isinstance(routes, Mapping):
        raise ConfigError(f"{path}.routes must be a mapping")

    actions: list[GogurtAction] = []
    for raw_route_name, raw_route in routes.items():
        route_name = str(raw_route_name).strip()
        if not isinstance(raw_route, Mapping):
            raise ConfigError(f"gogurt route {route_name!r} must be a mapping")
        if not bool(raw_route.get("enabled", True)):
            continue
        _validate_gogurt_route(route_name)
        actions.append(
            GogurtAction(route=route_name, command=_route_command(route_name, raw_route))
        )
    return actions


def _action_for_route(config_file: PathInput, route_name: str) -> GogurtAction:
    route_name = route_name.strip()
    routes = {action.route: action for action in load_gogurt_actions(config_file)}
    try:
        return routes[route_name]
    except KeyError as exc:
        available = ", ".join(sorted(routes)) or "none"
        raise ConfigError(
            f"gogurt route {route_name!r} is not configured (available: {available})"
        ) from exc


def route_for_gogurt_marker(config_file: PathInput, route_name: str) -> str:
    return _action_for_route(config_file, route_name).route


def _resolve_executable(command: list[str], config_dir: Path, actions_dir: Path | None) -> None:
    executable = command[0]
    if executable == sys.executable:
        return

    candidates: list[Path] = []
    if actions_dir is not None:
        candidates.append(actions_dir / executable)
    if "/" in executable or "\\" in executable:
        candidates.append(config_dir / executable)
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if not resolved.is_file():
            continue
        if os.name != "nt" and not os.access(resolved, os.X_OK):
            raise PermissionError(f"gogurt action is not executable: {resolved}")
        command[0] = str(resolved)
        return

    if actions_dir is not None:
        discovered_action = shutil.which(executable, path=str(actions_dir))
        if discovered_action is not None:
            command[0] = discovered_action
            return

    discovered = shutil.which(executable)
    if discovered is None:
        raise FileNotFoundError(f"gogurt action executable not found: {executable}")
    command[0] = discovered


def _resolve_command(
    action: GogurtAction,
    *,
    config_file: Path,
    mount_point: Path,
    actions_dir: Path | None,
) -> list[str]:
    config_dir = config_file.parent
    command: list[str] = []
    for token in action.command:
        rendered = token.replace("{config_dir}", str(config_dir)).replace(
            "{mount_point}", str(mount_point)
        )
        if "{config_dir}" in token:
            rendered = str(Path(rendered).resolve())
        command.append(rendered)
    if command[0] == "{python}":
        command[0] = sys.executable
    _resolve_executable(command, config_dir, actions_dir)
    return command


def plan_gogurt_action(
    config_file: PathInput,
    mount_point: PathInput,
    *,
    actions_dir: PathInput | None = None,
    marker_name: str = DEFAULT_GOGURT_MARKER_NAME,
) -> dict[str, object]:
    _validate_gogurt_marker_name(marker_name)
    config_path = Path(config_file).expanduser().resolve()
    root = Path(mount_point).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(root)

    marker = root / marker_name
    base: dict[str, object] = {
        "mount_point": str(root),
        "marker": str(marker),
        "marker_name": marker_name,
    }
    try:
        marker_value = read_gogurt_marker(marker)
    except FileNotFoundError:
        return {**base, "status": "unmarked"}
    action = _action_for_route(config_path, marker_value.route)
    actions_path = Path(actions_dir).expanduser().resolve() if actions_dir is not None else None
    command = _resolve_command(
        action,
        config_file=config_path,
        mount_point=root,
        actions_dir=actions_path,
    )
    return {
        **base,
        "status": "ready",
        "route": action.route,
        "command": command,
        "marker_identity": marker_value.identity,
    }


def execute_gogurt_action(
    plan: Mapping[str, object],
    *,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = revalidate_gogurt_action(plan)
    return subprocess.run(
        command,
        check=False,
        capture_output=capture_output,
        text=True,
    )


def revalidate_gogurt_action(plan: Mapping[str, object]) -> list[str]:
    """Return a planned argv only while its marker retains the observed identity."""

    if plan.get("status") != "ready":
        raise ValueError("gogurt action plan is not ready")
    raw_command = plan.get("command")
    if not isinstance(raw_command, list) or any(
        not isinstance(token, str) for token in raw_command
    ):
        raise ValueError("gogurt action plan has no command")
    raw_marker = plan.get("marker")
    raw_identity = plan.get("marker_identity")
    if not isinstance(raw_marker, str) or not isinstance(raw_identity, str):
        raise ValueError("gogurt action plan has no marker identity")
    current = read_gogurt_marker(raw_marker)
    if current.identity != raw_identity:
        raise ConfigError(f"gogurt marker changed before action execution: {raw_marker}")
    return list(raw_command)


def plan_gogurt_marker(
    config_file: PathInput,
    route_name: str,
    mount_point: PathInput,
    *,
    marker_name: str = DEFAULT_GOGURT_MARKER_NAME,
    force: bool = False,
) -> dict[str, object]:
    _validate_gogurt_marker_name(marker_name)
    marker_route = route_for_gogurt_marker(config_file, route_name)
    root = Path(mount_point).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(root)

    marker = root / marker_name
    text = f"{marker_route}\n"
    try:
        current = read_gogurt_marker(marker)
    except FileNotFoundError:
        current = None
    if current is not None and current.route != marker_route and not force:
        raise FileExistsError(f"gogurt marker already exists with different content: {marker}")
    if current is not None and current.route == marker_route:
        status = "would_keep"
    elif current is not None:
        status = "would_replace"
    else:
        status = "would_write"
    return {
        "dry_run": True,
        "status": status,
        "route": marker_route,
        "mount_point": str(root),
        "marker": str(marker),
        "marker_name": marker_name,
        "content": text,
        "force": force,
        "exists": current is not None,
    }


def write_gogurt_marker(
    config_file: PathInput,
    route_name: str,
    mount_point: PathInput,
    *,
    marker_name: str = DEFAULT_GOGURT_MARKER_NAME,
    force: bool = False,
) -> Path:
    plan = plan_gogurt_marker(
        config_file,
        route_name,
        mount_point,
        marker_name=marker_name,
        force=force,
    )
    marker = Path(str(plan["marker"]))
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(str(plan["content"]), encoding="utf-8")
        os.replace(temporary, marker)
    finally:
        temporary.unlink(missing_ok=True)
    return marker

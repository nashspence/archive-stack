from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import Any

from config_validation import ConfigError, load_yaml_config, validate_json_schema

from gogurt_core.mounts import (
    GOGURT_ROUTE_PATTERN,
    GogurtRouteMarker,
    MountedMarkerObservation,
    MountedVolumeProvider,
)
from gogurt_core.providers import GogurtProviderReference

DEFAULT_GOGURT_CONFIG_FILENAME = "gogurt-routes.yaml"
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
            "propertyNames": {"pattern": GOGURT_ROUTE_PATTERN},
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


def _read_gogurt_marker(
    provider: MountedVolumeProvider,
    mount_point: Path,
) -> MountedMarkerObservation | None:
    observation = provider.observe_marker(mount_point)
    if observation is not None and not isinstance(observation, MountedMarkerObservation):
        raise TypeError("Gogurt mounted-volume provider returned an invalid marker observation")
    return observation


def _provider_payload(provider: MountedVolumeProvider) -> dict[str, str]:
    reference = provider.reference
    if reference.kind != "mounted-volume":
        raise ValueError("Gogurt action requires a mounted-volume provider")
    return reference.as_dict()


def _require_plan_provider(
    plan: Mapping[str, object],
    provider: MountedVolumeProvider,
) -> None:
    raw = plan.get("mounted_volume_provider")
    try:
        planned = GogurtProviderReference.from_mapping(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("gogurt action plan has no mounted-volume provider identity") from exc
    if planned != provider.reference:
        raise ConfigError("gogurt action plan mounted-volume provider identity changed")


def default_gogurt_config_file(
    config_dir: PathInput,
    *,
    filename: str = DEFAULT_GOGURT_CONFIG_FILENAME,
) -> Path:
    return Path(config_dir).expanduser().resolve().parent / filename


def _validate_gogurt_route(route_name: str) -> None:
    try:
        GogurtRouteMarker(route_name)
    except (TypeError, ValueError) as exc:
        raise ConfigError(str(exc)) from exc


def _route_command(route_name: str, route: Mapping[str, Any]) -> tuple[str, ...]:
    raw_command = route.get("command")
    if (
        not isinstance(raw_command, list)
        or not raw_command
        or any(not isinstance(token, str) for token in raw_command)
    ):
        raise ConfigError(f"gogurt route {route_name!r}.command must be a nonempty list")
    command = tuple(raw_command)
    if any(not token.strip() for token in command):
        raise ConfigError(f"gogurt route {route_name!r}.command contains an empty token")
    if command.count("{mount_point}") != 1:
        raise ConfigError(
            f"gogurt route {route_name!r}.command must contain one {{mount_point}} token"
        )
    if command[0] == "{mount_point}":
        raise ConfigError(
            f"gogurt route {route_name!r}.command cannot use {{mount_point}} as its executable"
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
        if not isinstance(raw_route_name, str):
            raise ConfigError("gogurt route identifiers must be strings")
        route_name = raw_route_name
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


def validate_gogurt_action_executables(
    config_file: PathInput,
    *,
    actions_dir: PathInput | None = None,
) -> list[GogurtAction]:
    """Validate every action executable that does not depend on a mounted volume."""

    config_path = Path(config_file).expanduser().resolve()
    actions_path = Path(actions_dir).expanduser().resolve() if actions_dir is not None else None
    actions = load_gogurt_actions(config_path)
    validation_mount = config_path.parent / ".gogurt-validation-mount"
    for action in actions:
        if "{mount_point}" in action.command[0]:
            continue
        _resolve_command(
            action,
            config_file=config_path,
            mount_point=validation_mount,
            actions_dir=actions_path,
        )
    return actions


def plan_gogurt_action(
    config_file: PathInput,
    mount_point: PathInput,
    *,
    provider: MountedVolumeProvider,
    actions_dir: PathInput | None = None,
) -> dict[str, object]:
    config_path = Path(config_file).expanduser().resolve()
    root = Path(mount_point).expanduser().resolve()

    base: dict[str, object] = {
        "mount_point": str(root),
        "mounted_volume_provider": _provider_payload(provider),
    }
    marker_value = _read_gogurt_marker(provider, root)
    if marker_value is None:
        return {**base, "status": "unmarked"}
    action = _action_for_route(config_path, marker_value.marker.route)
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
        "marker": marker_value.marker.as_dict(),
        "marker_identity": marker_value.identity,
    }


def execute_gogurt_action(
    plan: Mapping[str, object],
    *,
    provider: MountedVolumeProvider,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = revalidate_gogurt_action(plan, provider=provider)
    return subprocess.run(
        command,
        check=False,
        capture_output=capture_output,
        text=True,
    )


def revalidate_gogurt_action(
    plan: Mapping[str, object],
    *,
    provider: MountedVolumeProvider,
) -> list[str]:
    """Return a planned argv only while its marker retains the observed identity."""

    if plan.get("status") != "ready":
        raise ValueError("gogurt action plan is not ready")
    _require_plan_provider(plan, provider)
    raw_command = plan.get("command")
    if not isinstance(raw_command, list) or any(
        not isinstance(token, str) for token in raw_command
    ):
        raise ValueError("gogurt action plan has no command")
    raw_mount = plan.get("mount_point")
    raw_identity = plan.get("marker_identity")
    if not isinstance(raw_mount, str) or not isinstance(raw_identity, str):
        raise ValueError("gogurt action plan has no marker identity")
    try:
        planned_marker = GogurtRouteMarker.from_mapping(plan.get("marker"))
    except (TypeError, ValueError) as exc:
        raise ValueError("gogurt action plan has no logical marker") from exc
    mount_point = Path(raw_mount)
    current = _read_gogurt_marker(provider, mount_point)
    if current is None or current.marker != planned_marker or current.identity != raw_identity:
        raise ConfigError(f"gogurt marker changed before action execution: {mount_point}")
    return list(raw_command)


def plan_gogurt_marker(
    config_file: PathInput,
    route_name: str,
    mount_point: PathInput,
    *,
    provider: MountedVolumeProvider,
    force: bool = False,
) -> dict[str, object]:
    marker_route = route_for_gogurt_marker(config_file, route_name)
    root = Path(mount_point).expanduser().resolve()

    marker = GogurtRouteMarker(marker_route)
    current = _read_gogurt_marker(provider, root)
    if current is not None and current.marker != marker and not force:
        raise FileExistsError(f"gogurt marker already exists with different content: {root}")
    if current is not None and current.marker == marker:
        status = "would_keep"
    elif current is not None:
        status = "would_replace"
    else:
        status = "would_write"
    plan: dict[str, object] = {
        "dry_run": True,
        "status": status,
        "route": marker_route,
        "mount_point": str(root),
        "marker": marker.as_dict(),
        "mounted_volume_provider": _provider_payload(provider),
        "force": force,
        "exists": current is not None,
    }
    if current is not None:
        plan["existing_marker_identity"] = current.identity
    return plan


def write_gogurt_marker(
    config_file: PathInput,
    route_name: str,
    mount_point: PathInput,
    *,
    provider: MountedVolumeProvider,
    force: bool = False,
) -> MountedMarkerObservation:
    plan = plan_gogurt_marker(
        config_file,
        route_name,
        mount_point,
        provider=provider,
        force=force,
    )
    if plan["status"] == "would_keep":
        current = _read_gogurt_marker(provider, Path(str(plan["mount_point"])))
        if (
            current is None
            or current.marker != GogurtRouteMarker(str(plan["route"]))
            or current.identity != plan.get("existing_marker_identity")
        ):
            raise ConfigError("gogurt marker changed before publication")
        return current
    _require_plan_provider(plan, provider)
    marker = GogurtRouteMarker(str(plan["route"]))
    observation = provider.publish_marker(
        Path(str(plan["mount_point"])),
        marker,
    )
    if not isinstance(observation, MountedMarkerObservation):
        raise TypeError("Gogurt mounted-volume provider returned an invalid marker observation")
    if observation.marker != marker:
        raise ConfigError("gogurt provider did not publish the requested logical marker")
    return observation

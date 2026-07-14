from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from munchy.config_schema import MUNCHY_DEVICE_PROFILE_SCHEMA
from riverhog_core.config_yaml import ConfigError, load_yaml_config, validate_json_schema

_PARAMETER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

_JOB_SECTION_KEYS = {
    "collection_slug",
    "destination_prefix",
    "output_mode",
    "tasks",
    "notify",
}


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Merge authored config mappings with nested dictionaries preserved."""

    merged = deepcopy(dict(base))
    for key, value in override.items():
        if key in merged and isinstance(merged[key], Mapping) and isinstance(value, Mapping):
            merged[str(key)] = deep_merge(merged[key], value)
        else:
            merged[str(key)] = deepcopy(value)
    return merged


def _render_string(value: str, *, parameters: Mapping[str, object], label: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in parameters:
            return match.group(0)
        return str(parameters[name])

    return _PARAMETER_RE.sub(replace, value)


def render_profile_value(value: Any, *, parameters: Mapping[str, object], label: str) -> Any:
    if isinstance(value, str):
        return _render_string(value, parameters=parameters, label=label)
    if isinstance(value, list):
        return [
            render_profile_value(item, parameters=parameters, label=f"{label}[]") for item in value
        ]
    if isinstance(value, Mapping):
        rendered: dict[str, Any] = {}
        for key, item in value.items():
            rendered_key = render_profile_value(
                str(key),
                parameters=parameters,
                label=f"{label}.key",
            )
            if not isinstance(rendered_key, str):
                raise ConfigError(f"{label} rendered a non-string key")
            rendered[rendered_key] = render_profile_value(
                item,
                parameters=parameters,
                label=f"{label}.{rendered_key}",
            )
        return rendered
    return deepcopy(value)


def _profile_parameters(
    profile: Mapping[str, Any],
    supplied: Mapping[str, object] | None,
    *,
    label: str,
) -> dict[str, object]:
    parameters: dict[str, object] = {}
    declarations = profile.get("parameters")
    if isinstance(declarations, Mapping):
        for name, raw_declaration in declarations.items():
            declaration = raw_declaration if isinstance(raw_declaration, Mapping) else {}
            if "default" in declaration:
                parameters[str(name)] = declaration["default"]
            if declaration.get("required") and name not in (supplied or {}):
                raise ConfigError(f"{label} requires device profile parameter {name!r}")
    if supplied:
        parameters.update({str(name): value for name, value in supplied.items()})
    return parameters


def load_device_profile(path: Path) -> dict[str, Any]:
    raw = load_yaml_config(path)
    validate_json_schema(raw, MUNCHY_DEVICE_PROFILE_SCHEMA, label=str(path))
    return raw


def instantiate_device_profile(
    profile: Mapping[str, Any],
    *,
    parameters: Mapping[str, object] | None = None,
    overrides: Mapping[str, Any] | None = None,
    label: str = "device_profile",
) -> dict[str, Any]:
    resolved_parameters = _profile_parameters(profile, parameters, label=label)
    section = profile.get("section")
    if not isinstance(section, Mapping):
        raise ConfigError(f"{label}.section must be a mapping")
    rendered = render_profile_value(
        section,
        parameters=resolved_parameters,
        label=f"{label}.section",
    )
    if not isinstance(rendered, Mapping):
        raise ConfigError(f"{label}.section rendered a non-mapping section")
    if not overrides:
        return deepcopy(dict(rendered))
    rendered_overrides = render_profile_value(
        overrides,
        parameters=resolved_parameters,
        label=f"{label}.overrides",
    )
    if not isinstance(rendered_overrides, Mapping):
        raise ConfigError(f"{label}.overrides rendered a non-mapping section")
    return deep_merge(rendered, rendered_overrides)


def resolve_profile_path(path: str, *, base_path: Path | None) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate
    if base_path is None:
        raise ConfigError(f"relative device_profile.path requires a config file path: {path}")
    return (base_path.parent / candidate).resolve()


def instantiate_device_profile_ref(
    ref: Mapping[str, Any],
    *,
    base_path: Path | None,
    label: str = "device_profile",
) -> dict[str, Any]:
    raw_path = ref.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ConfigError(f"{label}.path must be a non-empty string")
    profile_path = resolve_profile_path(raw_path, base_path=base_path)
    profile = load_device_profile(profile_path)
    raw_parameters = ref.get("parameters")
    if raw_parameters is not None and not isinstance(raw_parameters, Mapping):
        raise ConfigError(f"{label}.parameters must be a mapping")
    raw_overrides = ref.get("overrides")
    if raw_overrides is not None and not isinstance(raw_overrides, Mapping):
        raise ConfigError(f"{label}.overrides must be a mapping")
    return instantiate_device_profile(
        profile,
        parameters=raw_parameters,
        overrides=raw_overrides,
        label=str(profile_path),
    )


def _default_group_from_section(section: Mapping[str, Any]) -> dict[str, Any]:
    group_name = str(section.get("group") or "").strip()
    if not group_name:
        return {}
    group: dict[str, Any] = {}
    for key in ("output_mode", "tasks", "metadata_projection"):
        if key in section:
            group[key] = deepcopy(section[key])
    encode_profile = section.get("encode_profile")
    if isinstance(encode_profile, Mapping):
        group["encode_profile"] = deepcopy(dict(encode_profile))
    return {group_name: group}


def apply_device_profile_to_munchy_config(
    config: Mapping[str, Any],
    *,
    base_path: Path | None = None,
) -> dict[str, Any]:
    """Expand a device_profile reference into ordinary public Munchy job config."""

    ref = config.get("device_profile")
    if ref is None:
        return deepcopy(dict(config))
    if not isinstance(ref, Mapping):
        raise ConfigError("device_profile must be a mapping")

    section = instantiate_device_profile_ref(ref, base_path=base_path)
    expanded = deepcopy(dict(config))
    expanded.pop("device_profile", None)

    groups = _default_group_from_section(section)
    raw_section_groups = section.get("groups")
    if isinstance(raw_section_groups, Mapping):
        groups = deep_merge(groups, raw_section_groups)
    raw_config_groups = expanded.get("groups")
    if isinstance(raw_config_groups, Mapping):
        groups = deep_merge(groups, raw_config_groups)
    if groups:
        expanded["groups"] = groups

    raw_section_profiles = section.get("profiles")
    raw_config_profiles = expanded.get("profiles")
    if isinstance(raw_section_profiles, Mapping) or isinstance(raw_config_profiles, Mapping):
        expanded["profiles"] = deep_merge(
            raw_section_profiles if isinstance(raw_section_profiles, Mapping) else {},
            raw_config_profiles if isinstance(raw_config_profiles, Mapping) else {},
        )

    job_defaults = {key: deepcopy(section[key]) for key in _JOB_SECTION_KEYS if key in section}
    routing = section.get("routing")
    if isinstance(routing, Mapping):
        job_defaults["routing"] = deepcopy(dict(routing))

    raw_job = expanded.get("job")
    expanded["job"] = deep_merge(job_defaults, raw_job if isinstance(raw_job, Mapping) else {})
    return expanded


__all__ = [
    "apply_device_profile_to_munchy_config",
    "deep_merge",
    "instantiate_device_profile",
    "instantiate_device_profile_ref",
    "load_device_profile",
    "render_profile_value",
    "resolve_profile_path",
]

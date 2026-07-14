from __future__ import annotations

import math
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import jsonschema
import yaml


class ConfigError(ValueError):
    """Raised when an operator-authored YAML config is invalid."""


class StrictYamlLoader(yaml.SafeLoader):
    pass


def _construct_mapping(
    loader: StrictYamlLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[str, Any]:
    if not isinstance(node, yaml.MappingNode):
        raise yaml.constructor.ConstructorError(
            "while constructing a mapping",
            node.start_mark,
            "expected a mapping node",
            node.start_mark,
        )
    seen: set[str] = set()
    for key_node, _value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"expected string keys, found {type(key).__name__}",
                key_node.start_mark,
            )
        if key in seen:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        seen.add(key)
    return cast(dict[str, Any], yaml.SafeLoader.construct_mapping(loader, node, deep=deep))


StrictYamlLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def _json_value(value: object, *, label: str) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ConfigError(f"{label} contains non-string key {key!r}")
            out[key] = _json_value(item, label=f"{label}.{key}")
        return out
    if isinstance(value, list):
        return [_json_value(item, label=f"{label}[]") for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ConfigError(f"{label} contains non-finite number")
        return value
    raise ConfigError(f"{label} contains unsupported {type(value).__name__} value")


def load_yaml_config(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = yaml.load(handle, Loader=StrictYamlLoader)
    except OSError as exc:
        raise ConfigError(str(exc)) from exc
    except yaml.YAMLError as exc:
        raise ConfigError(str(exc)) from exc
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ConfigError("config must be a YAML mapping")
    return cast(dict[str, Any], _json_value(dict(raw), label=str(path)))


def validate_json_schema(
    data: Mapping[str, Any],
    schema: Mapping[str, Any],
    *,
    label: str,
) -> None:
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda error: list(error.absolute_path))
    if not errors:
        return
    error = errors[0]
    path = ".".join(str(part) for part in error.absolute_path)
    suffix = f"{path}: " if path else ""
    raise ConfigError(f"{label}: {suffix}{error.message}")


def normalize_authoring_routing(
    raw: Mapping[str, Any],
    *,
    label: str,
) -> dict[str, Any]:
    routing = deepcopy(dict(raw))
    sidecars = routing.get("sidecars")
    if isinstance(sidecars, Mapping):
        normalized_sidecars: list[dict[str, Any]] = []
        for sidecar_id, sidecar_raw in sidecars.items():
            if not isinstance(sidecar_raw, Mapping):
                raise ConfigError(f"{label}.sidecars.{sidecar_id} must be a mapping")
            sidecar = deepcopy(dict(sidecar_raw))
            if "id" in sidecar:
                raise ConfigError(f"{label}.sidecars.{sidecar_id} must not repeat id")
            sidecar["id"] = str(sidecar_id)
            normalized_sidecars.append(sidecar)
        routing["sidecars"] = normalized_sidecars
    return routing


def normalize_munchy_job_authoring(
    raw: Mapping[str, Any],
    *,
    label: str,
) -> dict[str, Any]:
    job = deepcopy(dict(raw))
    routing = job.get("routing")
    if routing is not None:
        if not isinstance(routing, Mapping):
            raise ConfigError(f"{label}.routing must be a mapping")
        job["routing"] = normalize_authoring_routing(routing, label=f"{label}.routing")
    return job

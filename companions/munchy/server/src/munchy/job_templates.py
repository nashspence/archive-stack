from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, cast

from config_validation import ConfigError, validate_json_schema
from munchy_config import MUNCHY_CONFIG_SCHEMA
from munchy_workflows.job_authoring import (
    MunchyJobAuthoringError,
    munchy_job_defaults_from_config,
    normalize_munchy_config,
)

JOB_TEMPLATE_RUNTIME_FIELDS = frozenset(
    {
        "collection_slug",
        "collection_timestamp",
        "destination_prefix",
        "input_upload_id",
        "job_id",
    }
)
JOB_TEMPLATE_INPUT_RE = re.compile(r"\{\{([A-Za-z][A-Za-z0-9_.-]*)\}\}")


class JobTemplateError(ValueError):
    """Raised when a server-owned Munchy job template is invalid."""


def _template_inputs(value: object) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise JobTemplateError("job template inputs must be an object")
    normalized: dict[str, dict[str, Any]] = {}
    for raw_name, raw_spec in value.items():
        name = str(raw_name).strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", name):
            raise JobTemplateError(f"invalid job template input name: {raw_name}")
        if not isinstance(raw_spec, Mapping):
            raise JobTemplateError(f"job template input {name} must be an object")
        spec: dict[str, Any] = {"required": bool(raw_spec.get("required", True))}
        choices = raw_spec.get("enum")
        if choices is not None:
            if not isinstance(choices, list) or not choices:
                raise JobTemplateError(f"job template input {name}.enum must not be empty")
            spec["enum"] = list(dict.fromkeys(str(item) for item in choices))
        normalized[name] = spec
    return normalized


def _input_placeholders(value: object) -> set[str]:
    if isinstance(value, str):
        return set(JOB_TEMPLATE_INPUT_RE.findall(value))
    if isinstance(value, Mapping):
        return set().union(*(_input_placeholders(item) for item in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_input_placeholders(item) for item in value), set())
    return set()


def render_job_template_inputs(
    definition: Mapping[str, Any],
    resolved_job: Mapping[str, Any],
    values: Mapping[str, str],
) -> dict[str, Any]:
    specs = _template_inputs(definition.get("inputs"))
    submitted = {str(name): str(value).strip() for name, value in values.items()}
    unknown = sorted(set(submitted) - set(specs))
    if unknown:
        raise JobTemplateError("unknown job template input(s): " + ", ".join(unknown))
    missing = sorted(
        name
        for name, spec in specs.items()
        if spec.get("required", True) and not submitted.get(name)
    )
    if missing:
        raise JobTemplateError("missing job template input(s): " + ", ".join(missing))
    for name, value in submitted.items():
        if not value:
            raise JobTemplateError(f"job template input {name} must not be blank")
        choices = specs[name].get("enum")
        if isinstance(choices, list) and value not in choices:
            raise JobTemplateError(
                f"job template input {name} must be one of: " + ", ".join(choices)
            )

    def render(value: object) -> Any:
        if isinstance(value, str):
            return JOB_TEMPLATE_INPUT_RE.sub(
                lambda match: submitted.get(match.group(1), match.group(0)),
                value,
            )
        if isinstance(value, Mapping):
            return {str(key): render(item) for key, item in value.items()}
        if isinstance(value, list):
            return [render(item) for item in value]
        return deepcopy(value)

    rendered = render(resolved_job)
    unresolved = sorted(_input_placeholders(rendered))
    if unresolved:
        raise JobTemplateError("unresolved job template input(s): " + ", ".join(unresolved))
    return cast(dict[str, Any], rendered)


def normalize_job_template(
    definition: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the canonical authored definition and resolved server job defaults."""

    raw = deepcopy(dict(definition))
    if raw.get("schema_version") != 1 or raw.get("kind") != "munchy.job":
        raise JobTemplateError("job template requires schema_version: 1 and kind: munchy.job")
    if "device_profile" in raw:
        raise JobTemplateError(
            "job templates must be expanded before upload; device_profile is an authoring input"
        )
    try:
        validate_json_schema(raw, MUNCHY_CONFIG_SCHEMA, label="job template")
        resolved_definition = normalize_munchy_config(raw)
        defaults = munchy_job_defaults_from_config(resolved_definition)
    except (ConfigError, MunchyJobAuthoringError) as exc:
        raise JobTemplateError(str(exc)) from exc
    configured_runtime_fields = sorted(JOB_TEMPLATE_RUNTIME_FIELDS.intersection(defaults))
    if configured_runtime_fields:
        raise JobTemplateError(
            "job template contains submission-owned field(s): "
            + ", ".join(configured_runtime_fields)
        )
    inputs = _template_inputs(raw.get("inputs"))
    placeholders = sorted(_input_placeholders(defaults))
    undeclared = sorted(set(placeholders) - set(inputs))
    if undeclared:
        raise JobTemplateError("job template uses undeclared input(s): " + ", ".join(undeclared))
    if inputs:
        raw["inputs"] = inputs
    return raw, defaults


def job_template_digest(definition: Mapping[str, Any]) -> str:
    encoded = json.dumps(definition, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "JOB_TEMPLATE_RUNTIME_FIELDS",
    "JobTemplateError",
    "job_template_digest",
    "normalize_job_template",
    "render_job_template_inputs",
]

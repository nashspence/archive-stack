from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from itertools import product
from typing import Any

from pydantic import ValidationError

from munchy_workflows.profiles import EncodeProfile

AXIS_ALIASES = {
    "quality": "archive.quality",
    "max_height": "archive.max_height",
    "audio_bitrate": "archive.audio.bitrate",
}
INT_RANGE_PATHS = {"archive.quality", "archive.max_height"}


def normalize_quality_values(qualities: list[int] | tuple[int, ...]) -> list[int]:
    if not qualities:
        raise ValueError("at least one quality is required")
    normalized: list[int] = []
    for quality in qualities:
        if quality < 0 or quality > 63:
            raise ValueError("quality must be between 0 and 63")
        if quality in normalized:
            raise ValueError(f"duplicate quality: {quality}")
        normalized.append(quality)
    return normalized


def parse_int_range_values(values: list[int | str] | tuple[int | str, ...]) -> list[int]:
    numbers: list[int] = []
    for raw in values:
        text = str(raw).strip()
        if not text:
            continue
        for part in text.split(","):
            item = part.strip()
            if not item:
                continue
            range_match = item.replace("-", "..", 1) if ".." not in item else item
            if ".." in range_match:
                bounds = range_match.split("..", 1)
                step = 1
                if ":" in bounds[1]:
                    end_text, step_text = bounds[1].split(":", 1)
                    bounds[1] = end_text
                    try:
                        step = int(step_text)
                    except ValueError as exc:
                        raise ValueError("integer range step must be an integer") from exc
                try:
                    start = int(bounds[0])
                    end = int(bounds[1])
                except ValueError as exc:
                    raise ValueError(
                        "value must be an integer or inclusive range like "
                        "24..60, 24-60, or 24..60:4"
                    ) from exc
                if step <= 0:
                    raise ValueError("integer range step must be positive")
                if start <= end:
                    numbers.extend(range(start, end + 1, step))
                else:
                    numbers.extend(range(start, end - 1, -step))
                continue
            try:
                numbers.append(int(item))
            except ValueError as exc:
                raise ValueError(
                    "value must be an integer or inclusive range like 24..60, 24-60, or 24..60:4"
                ) from exc
    if not numbers:
        raise ValueError("at least one value is required")
    normalized: list[int] = []
    for number in numbers:
        if number in normalized:
            raise ValueError(f"duplicate value: {number}")
        normalized.append(number)
    return normalized


def parse_quality_values(values: list[int | str] | tuple[int | str, ...]) -> list[int]:
    return normalize_quality_values(parse_int_range_values(values))


def normalize_axis_path(path: str) -> str:
    text = AXIS_ALIASES.get(str(path).strip(), str(path).strip())
    if not text or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", text):
        raise ValueError(f"invalid review sweep axis path: {path}")
    return text


def axis_template_key(path: str) -> str:
    return normalize_axis_path(path).replace(".", "_")


def normalize_axis_values(path: str, values: Any) -> list[Any]:
    axis_path = normalize_axis_path(path)
    raw_values = values if isinstance(values, list | tuple) else [values]
    if axis_path == "archive.quality":
        return parse_quality_values(raw_values)
    if axis_path in INT_RANGE_PATHS:
        return parse_int_range_values(raw_values)
    normalized: list[Any] = []
    for value in raw_values:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError(f"review sweep axis {axis_path} values must not be blank")
        if value in normalized:
            raise ValueError(f"duplicate review sweep axis value for {axis_path}: {value}")
        normalized.append(value)
    if not normalized:
        raise ValueError(f"review sweep axis {axis_path} requires at least one value")
    return normalized


def review_sweep_axes(sweep: Mapping[str, Any]) -> list[dict[str, Any]]:
    axes: list[dict[str, Any]] = []

    def add_axis(path: str, values: Any) -> None:
        axis_path = normalize_axis_path(path)
        axes.append({"path": axis_path, "values": normalize_axis_values(axis_path, values)})

    for key in ("quality", "max_height", "audio_bitrate"):
        if key in sweep:
            add_axis(key, sweep[key])

    raw_axes = sweep.get("axes")
    if isinstance(raw_axes, Mapping):
        for path, values in raw_axes.items():
            add_axis(str(path), values)
    elif isinstance(raw_axes, list):
        for index, raw_axis in enumerate(raw_axes):
            if not isinstance(raw_axis, Mapping):
                raise ValueError(f"review.sweep.axes[{index}] must be an object")
            path = str(raw_axis.get("path") or "").strip()
            if not path:
                raise ValueError(f"review.sweep.axes[{index}].path is required")
            if "values" not in raw_axis:
                raise ValueError(f"review.sweep.axes[{index}].values is required")
            add_axis(path, raw_axis["values"])
    elif raw_axes is not None:
        raise ValueError("review.sweep.axes must be an object or list")

    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for axis in axes:
        path = str(axis["path"])
        if path in seen:
            raise ValueError(f"duplicate review sweep axis: {path}")
        seen.add(path)
        out.append(axis)
    return out


def review_sweep_explicit_variants(sweep: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_variants = sweep.get("variants") or []
    if not isinstance(raw_variants, list):
        raise ValueError("review.sweep.variants must be a list")
    variants: list[dict[str, Any]] = []
    for index, raw_variant in enumerate(raw_variants):
        if not isinstance(raw_variant, Mapping):
            raise ValueError(f"review.sweep.variants[{index}] must be an object")
        settings = raw_variant.get("encode_settings") or {}
        if not isinstance(settings, Mapping):
            raise ValueError(f"review.sweep.variants[{index}].encode_settings must be an object")
        profile_id = str(raw_variant.get("profile_id") or "").strip()
        variant: dict[str, Any] = {"encode_settings": deepcopy(dict(settings))}
        if profile_id:
            variant["profile_id"] = profile_id
        variants.append(variant)
    return variants


def ensure_review_sweep_has_variants(sweep: Mapping[str, Any]) -> None:
    if not review_sweep_axes(sweep) and not review_sweep_explicit_variants(sweep):
        raise ValueError("review.sweep requires axes, variants, or a shorthand axis")


def review_output_mode_for_profile(profile: Mapping[str, Any]) -> str:
    archive = profile.get("archive")
    if not isinstance(archive, Mapping):
        return "video"
    if str(archive.get("codec") or "").strip() == "opus":
        return "audio"
    return "video"


def review_tasks_for_output_mode(output_mode: str) -> list[str]:
    if output_mode == "audio":
        return ["audio_review"]
    if output_mode == "video":
        return ["qcut_video"]
    return []


def deep_merge_encode_settings(
    base: Mapping[str, Any],
    override: Mapping[str, Any],
) -> dict[str, Any]:
    merged = deepcopy(dict(base))
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = deep_merge_encode_settings(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def set_encode_setting(profile: dict[str, Any], path: str, value: Any) -> None:
    parts = normalize_axis_path(path).split(".")
    node: dict[str, Any] = profile
    for part in parts[:-1]:
        child = node.setdefault(part, {})
        if not isinstance(child, dict):
            raise ValueError(f"cannot set review sweep axis below non-object path: {path}")
        node = child
    node[parts[-1]] = deepcopy(value)


def encode_profile_with_settings(
    profile: Mapping[str, Any],
    *,
    name: str,
    settings: Mapping[str, Any],
) -> dict[str, Any]:
    variant = deepcopy(dict(profile))
    variant.setdefault("schema_version", 1)
    variant = deep_merge_encode_settings(variant, settings)
    variant["name"] = name
    try:
        return EncodeProfile.model_validate(variant).server_payload()
    except ValidationError as exc:
        raise ValueError(f"invalid review sweep encode profile: {exc}") from exc


def default_encode_profile_for_output_mode(output_mode: str) -> dict[str, Any]:
    if output_mode == "audio":
        return {
            "schema_version": 1,
            "target": "munchy-audio",
            "archive": {"codec": "opus", "container": "opus"},
        }
    return {
        "schema_version": 1,
        "target": "munchy-av1-nvenc",
        "archive": {"codec": "av1", "container": "mkv"},
    }


def profile_id_value(value: Any) -> str:
    text = str(value).strip()
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip(".-_")
    return text or "value"


def axis_profile_id_part(path: str, value: Any) -> str:
    normalized_path = normalize_axis_path(path)
    prefix = {
        "archive.quality": "q",
        "archive.max_height": "h",
        "archive.audio.bitrate": "a",
    }.get(normalized_path)
    value_text = profile_id_value(value)
    if prefix is not None:
        return f"{prefix}{value_text}"
    return f"{axis_template_key(normalized_path)}-{value_text}"


def render_profile_id_template(
    template: str,
    *,
    route_id: str,
    variant_index: int,
    settings: Mapping[str, Any],
    axis_values: Mapping[str, Any],
) -> str:
    mapping: dict[str, Any] = {
        "route_id": route_id,
        "variant_index": variant_index,
    }
    for path, value in axis_values.items():
        key = axis_template_key(path)
        mapping[key] = profile_id_value(value)
    if "archive.quality" in axis_values:
        mapping["quality"] = profile_id_value(axis_values["archive.quality"])
    if "archive.max_height" in axis_values:
        mapping["max_height"] = profile_id_value(axis_values["archive.max_height"])
    if "archive.audio.bitrate" in axis_values:
        mapping["audio_bitrate"] = profile_id_value(axis_values["archive.audio.bitrate"])
    for key, value in settings.items():
        mapping.setdefault(str(key), profile_id_value(value))
    try:
        rendered = template.format(**mapping)
    except KeyError as exc:
        raise ValueError(f"unknown review sweep profile_id_template field: {exc.args[0]}") from exc
    return profile_id_value(rendered)


def generated_profile_id(axis_values: Mapping[str, Any], *, variant_index: int) -> str:
    if axis_values:
        return "-".join(axis_profile_id_part(path, value) for path, value in axis_values.items())
    return f"variant-{variant_index:03d}"


def axis_settings(axis_values: Mapping[str, Any]) -> dict[str, Any]:
    settings: dict[str, Any] = {}
    for path, value in axis_values.items():
        set_encode_setting(settings, path, value)
    return settings


def review_sweep_variants(
    sweep: Mapping[str, Any],
    *,
    base_profile: Mapping[str, Any],
    route_id: str,
) -> list[dict[str, Any]]:
    axes = review_sweep_axes(sweep)
    explicit_variants = review_sweep_explicit_variants(sweep)
    template = str(sweep.get("profile_id_template") or "").strip()
    variants: list[dict[str, Any]] = []

    if axes:
        paths = [str(axis["path"]) for axis in axes]
        values_by_axis = [list(axis["values"]) for axis in axes]
        for values in product(*values_by_axis):
            axis_values = dict(zip(paths, values, strict=True))
            settings = axis_settings(axis_values)
            variant_index = len(variants) + 1
            profile_id = (
                render_profile_id_template(
                    template,
                    route_id=route_id,
                    variant_index=variant_index,
                    settings=settings,
                    axis_values=axis_values,
                )
                if template
                else generated_profile_id(axis_values, variant_index=variant_index)
            )
            variants.append(
                {
                    "profile_id": profile_id,
                    "encode_settings": settings,
                    "axis_values": axis_values,
                    "encode_profile": encode_profile_with_settings(
                        base_profile,
                        name=profile_id,
                        settings=settings,
                    ),
                }
            )

    for explicit in explicit_variants:
        settings = explicit["encode_settings"]
        variant_index = len(variants) + 1
        profile_id = str(explicit.get("profile_id") or "")
        if not profile_id:
            profile_id = (
                render_profile_id_template(
                    template,
                    route_id=route_id,
                    variant_index=variant_index,
                    settings=settings,
                    axis_values={},
                )
                if template
                else generated_profile_id({}, variant_index=variant_index)
            )
        profile_id = profile_id_value(profile_id)
        variants.append(
            {
                "profile_id": profile_id,
                "encode_settings": settings,
                "axis_values": {},
                "encode_profile": encode_profile_with_settings(
                    base_profile,
                    name=profile_id,
                    settings=settings,
                ),
            }
        )

    seen: set[str] = set()
    for variant in variants:
        profile_id = str(variant["profile_id"])
        if profile_id in seen:
            raise ValueError(f"duplicate review sweep profile_id: {profile_id}")
        seen.add(profile_id)
    return variants

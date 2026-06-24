from __future__ import annotations

import fnmatch
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast


@dataclass(frozen=True)
class ProfileRouteMatch:
    route_id: str
    group: str
    route: Mapping[str, Any]
    index: int


def match_profile_route(
    profile_routing: Mapping[str, Any],
    rel_path: str,
    *,
    probe_summary_loader: Callable[[], Mapping[str, Any]] | None = None,
) -> ProfileRouteMatch | None:
    probe_summary: Mapping[str, Any] | None = None
    for index, route in enumerate(profile_routes(profile_routing)):
        if not route_matches_path(route, rel_path):
            continue
        if route_requires_probe(route):
            if probe_summary_loader is None:
                continue
            if probe_summary is None:
                probe_summary = probe_summary_loader()
            if not route_matches_probe(route, probe_summary):
                continue
        return ProfileRouteMatch(
            route_id=str(route.get("id") or f"route-{index + 1}"),
            group=str(route.get("group") or ""),
            route=route,
            index=index,
        )
    return None


def profile_routes(profile_routing: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    routes = profile_routing.get("routes")
    if not isinstance(routes, list):
        return []
    return [route for route in routes if isinstance(route, Mapping)]


def routing_probe_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw_streams = payload.get("streams")
    streams: Sequence[object] = raw_streams if isinstance(raw_streams, list) else ()
    video_stream = next(
        (
            stream
            for stream in streams
            if isinstance(stream, Mapping) and stream.get("codec_type") == "video"
        ),
        {},
    )
    format_info = payload.get("format") if isinstance(payload.get("format"), Mapping) else {}
    return {
        "format_name": str(cast(Mapping[str, Any], format_info).get("format_name") or ""),
        "format_tags": lower_mapping(cast(Mapping[str, Any], format_info).get("tags")),
        "stream_tags": lower_mapping(cast(Mapping[str, Any], video_stream).get("tags")),
        "codec_name": str(cast(Mapping[str, Any], video_stream).get("codec_name") or "").lower(),
        "width": int(cast(Mapping[str, Any], video_stream).get("width") or 0),
        "height": int(cast(Mapping[str, Any], video_stream).get("height") or 0),
        "fps": parse_rate(cast(Mapping[str, Any], video_stream).get("avg_frame_rate"))
        or parse_rate(cast(Mapping[str, Any], video_stream).get("r_frame_rate"))
        or 0.0,
    }


def route_requires_probe(route: Mapping[str, Any]) -> bool:
    probe_keys = {
        "format_name_contains",
        "codec_names",
        "width",
        "height",
        "min_width",
        "max_width",
        "min_height",
        "max_height",
        "fps",
        "min_fps",
        "max_fps",
        "format_tags",
        "stream_tags",
    }
    return any(route.get(key) for key in probe_keys)


def route_matches_path(route: Mapping[str, Any], rel_path: str) -> bool:
    filename = rel_path.rsplit("/", 1)[-1]
    prefix = route.get("path_prefix")
    if isinstance(prefix, str) and prefix:
        normalized = prefix.rstrip("/")
        if rel_path != normalized and not rel_path.startswith(f"{normalized}/"):
            return False
    path_glob = route.get("path_glob")
    if isinstance(path_glob, str) and path_glob and not fnmatch.fnmatchcase(rel_path, path_glob):
        return False
    filename_glob = route.get("filename_glob")
    if (
        isinstance(filename_glob, str)
        and filename_glob
        and not fnmatch.fnmatchcase(filename, filename_glob)
    ):
        return False
    suffixes = [str(item).lower() for item in route.get("suffixes") or []]
    if suffixes and not any(filename.lower().endswith(suffix) for suffix in suffixes):
        return False
    return True


def route_matches_probe(route: Mapping[str, Any], summary: Mapping[str, Any]) -> bool:
    format_needles = [str(item).lower() for item in route.get("format_name_contains") or []]
    format_name = str(summary.get("format_name") or "").lower()
    if format_needles and not all(needle in format_name for needle in format_needles):
        return False
    codec_names = [str(item).lower() for item in route.get("codec_names") or []]
    if codec_names and str(summary.get("codec_name") or "").lower() not in codec_names:
        return False
    width = int(summary.get("width") or 0)
    height = int(summary.get("height") or 0)
    fps = float(summary.get("fps") or 0.0)
    for key, actual_dimension in (("width", width), ("height", height)):
        expected = route.get(key)
        if expected is not None and actual_dimension != int(cast(int, expected)):
            return False
    numeric_limits: tuple[tuple[str, float, Literal["min", "max"]], ...] = (
        ("min_width", float(width), "min"),
        ("max_width", float(width), "max"),
        ("min_height", float(height), "min"),
        ("max_height", float(height), "max"),
        ("min_fps", fps, "min"),
        ("max_fps", fps, "max"),
    )
    for key, actual_limit, op in numeric_limits:
        expected = route.get(key)
        if expected is None:
            continue
        if op == "min" and actual_limit < float(cast(float, expected)):
            return False
        if op == "max" and actual_limit > float(cast(float, expected)):
            return False
    expected_fps = route.get("fps")
    if expected_fps is not None and abs(fps - float(cast(float, expected_fps))) > 0.01:
        return False

    for tag_scope in ("format_tags", "stream_tags"):
        expected_tags = route.get(tag_scope) or {}
        if not isinstance(expected_tags, Mapping) or not expected_tags:
            continue
        actual_tags = summary.get(tag_scope) if isinstance(summary.get(tag_scope), Mapping) else {}
        lower_actual = lower_mapping(actual_tags)
        for key, expected in expected_tags.items():
            actual_value = str(lower_actual.get(str(key).lower()) or "")
            if str(expected).lower() not in actual_value:
                return False
    return True


def lower_mapping(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key).strip().lower(): str(item).strip().lower()
        for key, item in value.items()
        if str(key).strip() and str(item).strip()
    }


def parse_rate(value: object) -> float | None:
    if value in (None, "", "0/0"):
        return None
    text = str(value)
    if "/" in text:
        numerator, denominator = text.split("/", 1)
        try:
            denom = float(denominator)
            if denom == 0:
                return None
            return float(numerator) / denom
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None

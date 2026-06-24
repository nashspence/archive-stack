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
    video_streams = [
        cast(Mapping[str, Any], stream)
        for stream in streams
        if isinstance(stream, Mapping) and stream.get("codec_type") == "video"
    ]
    audio_streams = [
        cast(Mapping[str, Any], stream)
        for stream in streams
        if isinstance(stream, Mapping) and stream.get("codec_type") == "audio"
    ]
    video_stream = video_streams[0] if video_streams else {}
    audio_stream = audio_streams[0] if audio_streams else {}
    format_info = payload.get("format") if isinstance(payload.get("format"), Mapping) else {}
    format_mapping = cast(Mapping[str, Any], format_info)
    format_duration = parse_float(format_mapping.get("duration"))
    video_duration = parse_float(video_stream.get("duration"))
    audio_duration = parse_float(audio_stream.get("duration"))
    duration = first_number(video_duration, format_duration, audio_duration) or 0.0
    return {
        "format_name": str(format_mapping.get("format_name") or "").lower(),
        "format_long_name": str(format_mapping.get("format_long_name") or "").lower(),
        "format_duration": format_duration or 0.0,
        "format_bit_rate": parse_int(format_mapping.get("bit_rate")) or 0,
        "format_tags": lower_mapping(format_mapping.get("tags")),
        "duration": duration,
        "has_video": bool(video_streams),
        "has_audio": bool(audio_streams),
        "video_stream_count": len(video_streams),
        "audio_stream_count": len(audio_streams),
        "codec_name": str(video_stream.get("codec_name") or "").lower(),
        "video_codec_name": str(video_stream.get("codec_name") or "").lower(),
        "video_codec_long_name": str(video_stream.get("codec_long_name") or "").lower(),
        "video_profile": str(video_stream.get("profile") or "").lower(),
        "video_pix_fmt": str(video_stream.get("pix_fmt") or "").lower(),
        "video_color_range": str(video_stream.get("color_range") or "").lower(),
        "video_color_space": str(video_stream.get("color_space") or "").lower(),
        "video_color_transfer": str(video_stream.get("color_transfer") or "").lower(),
        "video_color_primaries": str(video_stream.get("color_primaries") or "").lower(),
        "width": parse_int(video_stream.get("width")) or 0,
        "height": parse_int(video_stream.get("height")) or 0,
        "fps": parse_rate(video_stream.get("avg_frame_rate"))
        or parse_rate(video_stream.get("r_frame_rate"))
        or 0.0,
        "video_duration": video_duration or 0.0,
        "video_bit_rate": parse_int(video_stream.get("bit_rate")) or 0,
        "video_tags": lower_mapping(video_stream.get("tags")),
        "stream_tags": lower_mapping(video_stream.get("tags")),
        "audio_codec_name": str(audio_stream.get("codec_name") or "").lower(),
        "audio_codec_long_name": str(audio_stream.get("codec_long_name") or "").lower(),
        "audio_profile": str(audio_stream.get("profile") or "").lower(),
        "audio_sample_rate": parse_int(audio_stream.get("sample_rate")) or 0,
        "audio_channels": parse_int(audio_stream.get("channels")) or 0,
        "audio_channel_layout": str(audio_stream.get("channel_layout") or "").lower(),
        "audio_duration": audio_duration or 0.0,
        "audio_bit_rate": parse_int(audio_stream.get("bit_rate")) or 0,
        "audio_tags": lower_mapping(audio_stream.get("tags")),
    }


def route_requires_probe(route: Mapping[str, Any]) -> bool:
    probe_keys = {
        "format_names",
        "format_name_contains",
        "codec_names",
        "video_codec_names",
        "audio_codec_names",
        "video_profiles",
        "pix_fmts",
        "color_ranges",
        "color_spaces",
        "color_transfers",
        "color_primaries",
        "has_video",
        "has_audio",
        "video_stream_count",
        "audio_stream_count",
        "min_video_stream_count",
        "max_video_stream_count",
        "min_audio_stream_count",
        "max_audio_stream_count",
        "width",
        "height",
        "min_width",
        "max_width",
        "min_height",
        "max_height",
        "duration",
        "min_duration",
        "max_duration",
        "bit_rate",
        "min_bit_rate",
        "max_bit_rate",
        "fps",
        "min_fps",
        "max_fps",
        "audio_sample_rate",
        "min_audio_sample_rate",
        "max_audio_sample_rate",
        "audio_channels",
        "min_audio_channels",
        "max_audio_channels",
        "format_tags",
        "stream_tags",
        "video_tags",
        "audio_tags",
    }
    return any(route_has_condition(route, key) for key in probe_keys)


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
    format_names = [str(item).lower() for item in route.get("format_names") or []]
    if format_names and str(summary.get("format_name") or "").lower() not in format_names:
        return False
    format_needles = [str(item).lower() for item in route.get("format_name_contains") or []]
    format_name = str(summary.get("format_name") or "").lower()
    if format_needles and not all(needle in format_name for needle in format_needles):
        return False
    for route_key, summary_key in (
        ("codec_names", "codec_name"),
        ("video_codec_names", "video_codec_name"),
        ("audio_codec_names", "audio_codec_name"),
        ("video_profiles", "video_profile"),
        ("pix_fmts", "video_pix_fmt"),
        ("color_ranges", "video_color_range"),
        ("color_spaces", "video_color_space"),
        ("color_transfers", "video_color_transfer"),
        ("color_primaries", "video_color_primaries"),
    ):
        expected_values = [str(item).lower() for item in route.get(route_key) or []]
        if expected_values and str(summary.get(summary_key) or "").lower() not in expected_values:
            return False
    for key in ("has_video", "has_audio"):
        expected_bool = route.get(key)
        if expected_bool is not None and bool(summary.get(key)) != bool(expected_bool):
            return False
    numeric_equals: tuple[tuple[str, str, Literal["int", "float"]], ...] = (
        ("video_stream_count", "video_stream_count", "int"),
        ("audio_stream_count", "audio_stream_count", "int"),
        ("width", "width", "int"),
        ("height", "height", "int"),
        ("duration", "duration", "float"),
        ("bit_rate", "format_bit_rate", "int"),
        ("fps", "fps", "float"),
        ("audio_sample_rate", "audio_sample_rate", "int"),
        ("audio_channels", "audio_channels", "int"),
    )
    for route_key, summary_key, kind in numeric_equals:
        expected = route.get(route_key)
        if expected is None:
            continue
        actual = summary.get(summary_key) or 0
        if kind == "int" and int(actual) != int(cast(int, expected)):
            return False
        if kind == "float" and abs(float(actual) - float(cast(float, expected))) > 0.01:
            return False
    numeric_limits: tuple[tuple[str, str, Literal["min", "max"]], ...] = (
        ("min_video_stream_count", "video_stream_count", "min"),
        ("max_video_stream_count", "video_stream_count", "max"),
        ("min_audio_stream_count", "audio_stream_count", "min"),
        ("max_audio_stream_count", "audio_stream_count", "max"),
        ("min_width", "width", "min"),
        ("max_width", "width", "max"),
        ("min_height", "height", "min"),
        ("max_height", "height", "max"),
        ("min_duration", "duration", "min"),
        ("max_duration", "duration", "max"),
        ("min_bit_rate", "format_bit_rate", "min"),
        ("max_bit_rate", "format_bit_rate", "max"),
        ("min_fps", "fps", "min"),
        ("max_fps", "fps", "max"),
        ("min_audio_sample_rate", "audio_sample_rate", "min"),
        ("max_audio_sample_rate", "audio_sample_rate", "max"),
        ("min_audio_channels", "audio_channels", "min"),
        ("max_audio_channels", "audio_channels", "max"),
    )
    for key, summary_key, op in numeric_limits:
        expected = route.get(key)
        if expected is None:
            continue
        actual_limit = float(summary.get(summary_key) or 0.0)
        if op == "min" and actual_limit < float(cast(float, expected)):
            return False
        if op == "max" and actual_limit > float(cast(float, expected)):
            return False

    for tag_scope in ("format_tags", "stream_tags", "video_tags", "audio_tags"):
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


def route_has_condition(route: Mapping[str, Any], key: str) -> bool:
    if key not in route:
        return False
    value = route.get(key)
    if value is None:
        return False
    if isinstance(value, (str, Sequence, Mapping)) and not isinstance(value, bool):
        return bool(value)
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


def parse_float(value: object) -> float | None:
    if value in (None, "", "N/A"):
        return None
    try:
        return float(str(value))
    except ValueError:
        return None


def parse_int(value: object) -> int | None:
    parsed = parse_float(value)
    if parsed is None:
        return None
    return int(parsed)


def first_number(*values: float | None) -> float | None:
    return next((value for value in values if value is not None and value > 0), None)

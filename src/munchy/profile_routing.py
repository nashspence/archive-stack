from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Literal, cast

RouteAction = Literal["upload", "leave"]

PROBE_FACT_PREFIXES = ("ffprobe.", "video.", "audio.")
EXIFTOOL_FACT_PREFIXES = ("exif.", "exiftool.")


@dataclass(frozen=True)
class ProfileRouteMatch:
    route_id: str
    group: str
    route: Mapping[str, Any]
    index: int
    action: RouteAction = "upload"
    into: str | None = None
    collection_rel_path: str | None = None
    pair_kind: str | None = None
    pairing_id: str | None = None
    pair_role: str | None = None
    pair_with: str | None = None
    facts: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProfileRoutingFile:
    path: str
    bytes: int = 0
    sha256: str | None = None
    probe_summary: Mapping[str, Any] | None = None
    probe_error: str | None = None
    routing_facts: Mapping[str, Any] | None = None
    facts_error: str | None = None


@dataclass(frozen=True)
class ProfileRoutingPlan:
    ok: bool
    files_total: int
    matched_files: int
    left_files: int
    unmatched_files: int
    matches: list[dict[str, Any]] = field(default_factory=list)
    left: list[dict[str, Any]] = field(default_factory=list)
    unmatched: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "files_total": self.files_total,
            "matched_files": self.matched_files,
            "left_files": self.left_files,
            "unmatched_files": self.unmatched_files,
            "matches": self.matches,
            "left": self.left,
            "unmatched": self.unmatched,
        }


def match_profile_route(
    profile_routing: Mapping[str, Any],
    rel_path: str,
    *,
    probe_summary_loader: Callable[[], Mapping[str, Any]] | None = None,
    routing_facts_loader: Callable[[], Mapping[str, Any]] | None = None,
    routing_facts: Mapping[str, Any] | None = None,
) -> ProfileRouteMatch | None:
    facts = routing_file_facts(rel_path, routing_facts=routing_facts)
    facts_loaded = routing_facts is not None
    probe_loaded = bool(
        fact_value(facts, "ffprobe.format_name")
        or fact_value(facts, "video.has_video") is not None
        or fact_value(facts, "audio.has_audio") is not None
    )

    def ensure_probe_facts() -> Mapping[str, Any]:
        nonlocal facts, probe_loaded
        if not probe_loaded and probe_summary_loader is not None:
            facts = routing_file_facts(
                rel_path,
                probe_summary=probe_summary_loader(),
                routing_facts=facts,
            )
            probe_loaded = True
        return facts

    def ensure_full_facts() -> Mapping[str, Any]:
        nonlocal facts, facts_loaded
        if not facts_loaded and routing_facts_loader is not None:
            facts = routing_file_facts(rel_path, routing_facts=routing_facts_loader())
            facts_loaded = True
        return facts

    for index, route in enumerate(profile_routes(profile_routing)):
        route_facts: Mapping[str, Any] = facts
        if route_requires_probe(route, profile_routing=profile_routing):
            route_facts = ensure_probe_facts()
        if route_requires_exiftool(route, profile_routing=profile_routing):
            route_facts = ensure_full_facts()
        if not route_matches(route, route_facts, profile_routing=profile_routing):
            continue
        action = route_action(route)
        into = optional_normalized_dir(route.get("into"))
        collection_rel_path = route_collection_rel_path(route, rel_path)
        return ProfileRouteMatch(
            route_id=str(route.get("id") or f"route-{index + 1}"),
            group=str(route.get("group") or ""),
            route=route,
            index=index,
            action=action,
            into=into,
            collection_rel_path=collection_rel_path,
            pair_kind=optional_fact(route_facts, "pair.kind"),
            pairing_id=optional_fact(route_facts, "pair.id"),
            pair_role=optional_fact(route_facts, "pair.role"),
            pair_with=optional_fact(route_facts, "pair.with"),
            facts=route_facts,
        )
    return None


def profile_routing_plan(
    profile_routing: Mapping[str, Any],
    files: Sequence[ProfileRoutingFile],
    *,
    group_names: set[str] | None = None,
) -> ProfileRoutingPlan:
    facts_by_path = {
        item.path: routing_file_facts(
            item.path,
            probe_summary=item.probe_summary,
            routing_facts=item.routing_facts,
        )
        for item in files
    }
    facts_by_path = apply_pairing_rules(profile_routing, facts_by_path)

    matches: list[dict[str, Any]] = []
    left: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    for item in files:
        facts = facts_by_path[item.path]
        match = match_profile_route(profile_routing, item.path, routing_facts=facts)
        if match is None:
            unmatched.append(
                {
                    "path": item.path,
                    "bytes": item.bytes,
                    "reason": unmatched_reason(item, profile_routing),
                    "probe_error": item.probe_error,
                    "facts_error": item.facts_error,
                }
            )
            continue
        common = {
            "path": item.path,
            "bytes": item.bytes,
            "route_id": match.route_id,
            "route_index": match.index,
            "action": match.action,
            "pair_kind": match.pair_kind,
            "pairing_id": match.pairing_id,
            "pair_role": match.pair_role,
            "pair_with": match.pair_with,
            "matched_facts": matched_fact_values(
                match.route,
                match.facts,
                profile_routing=profile_routing,
            ),
        }
        if match.action == "leave":
            left.append(common)
            continue
        if not match.group:
            unmatched.append({**common, "reason": "missing_group"})
            continue
        if group_names is not None and match.group not in group_names:
            unmatched.append(
                {
                    **common,
                    "group": match.group,
                    "reason": f"unknown_group:{match.group}",
                }
            )
            continue
        matches.append(
            {
                **common,
                "group": match.group,
                "into": match.into,
                "collection_rel_path": match.collection_rel_path or item.path,
            }
        )
    return ProfileRoutingPlan(
        ok=not unmatched,
        files_total=len(files),
        matched_files=len(matches),
        left_files=len(left),
        unmatched_files=len(unmatched),
        matches=matches,
        left=left,
        unmatched=unmatched,
    )


def unmatched_reason(item: ProfileRoutingFile, profile_routing: Mapping[str, Any]) -> str:
    if item.facts_error and profile_routing_requires_exiftool(profile_routing):
        return "facts_failed"
    if item.probe_error and profile_routing_requires_probe(profile_routing):
        return "probe_failed"
    return "no_matching_route"


def profile_routes(profile_routing: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    routes = profile_routing.get("routes")
    if not isinstance(routes, list):
        return []
    return [route for route in routes if isinstance(route, Mapping)]


def profile_routing_requires_probe(profile_routing: Mapping[str, Any]) -> bool:
    return routing_uses_fact_prefix(profile_routing, PROBE_FACT_PREFIXES)


def profile_routing_requires_exiftool(profile_routing: Mapping[str, Any]) -> bool:
    return routing_uses_fact_prefix(profile_routing, EXIFTOOL_FACT_PREFIXES)


def route_requires_probe(
    route: Mapping[str, Any],
    *,
    profile_routing: Mapping[str, Any] | None = None,
) -> bool:
    return predicate_uses_fact_prefix(
        mapping(route.get("when")),
        PROBE_FACT_PREFIXES,
        profile_routing=profile_routing,
    )


def route_requires_exiftool(
    route: Mapping[str, Any],
    *,
    profile_routing: Mapping[str, Any] | None = None,
) -> bool:
    return predicate_uses_fact_prefix(
        mapping(route.get("when")),
        EXIFTOOL_FACT_PREFIXES,
        profile_routing=profile_routing,
    )


def routing_file_facts(
    rel_path: str,
    *,
    probe_summary: Mapping[str, Any] | None = None,
    exiftool_summary: Mapping[str, Any] | None = None,
    routing_facts: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    facts.update(path_facts(rel_path))
    if probe_summary:
        facts["ffprobe"] = dict(probe_summary)
        facts.update(probe_facts(probe_summary))
    if exiftool_summary:
        facts["exiftool"] = dict(exiftool_summary)
        facts.update(exiftool_facts(exiftool_summary))
    if routing_facts:
        facts.update(flatten_facts(routing_facts))
    return facts


def path_facts(rel_path: str) -> dict[str, Any]:
    normalized = normalize_posix_path(rel_path)
    path = PurePosixPath(normalized)
    suffix = path.suffix.lower()
    stem = path.stem
    return {
        "path": normalized,
        "path.rel": normalized,
        "path.basename": path.name,
        "path.filename": path.name,
        "path.stem": stem,
        "path.stem_lower": stem.lower(),
        "path.suffix": suffix,
        "path.extension": suffix.lstrip("."),
        "path.parent": path.parent.as_posix() if path.parent.as_posix() != "." else "",
        "path.parts": list(path.parts),
    }


def probe_facts(summary: Mapping[str, Any]) -> dict[str, Any]:
    width = parse_int(summary.get("width")) or 0
    height = parse_int(summary.get("height")) or 0
    fps = parse_float(summary.get("fps")) or 0.0
    codec_name = str(summary.get("video_codec_name") or summary.get("codec_name") or "").lower()
    codec_tag = str(summary.get("codec_tag_string") or summary.get("video_codec_tag_string") or "")
    return {
        "ffprobe.format_name": str(summary.get("format_name") or "").lower(),
        "ffprobe.format_long_name": str(summary.get("format_long_name") or "").lower(),
        "ffprobe.duration": parse_float(summary.get("duration")) or 0.0,
        "video.width": width,
        "video.height": height,
        "video.long_edge": max(width, height),
        "video.short_edge": min(width, height) if width and height else 0,
        "video.aspect_ratio": (
            max(width, height) / min(width, height) if width and height else 0.0
        ),
        "video.resolution": video_resolution_label(width, height),
        "video.fps": round(fps),
        "video.fps_exact": fps,
        "video.codec_name": codec_name,
        "video.codec": video_codec_label(codec_name, codec_tag),
        "video.has_video": bool(summary.get("has_video")),
        "audio.has_audio": bool(summary.get("has_audio")),
        "audio.codec_name": str(summary.get("audio_codec_name") or "").lower(),
        "audio.channels": parse_int(summary.get("audio_channels")) or 0,
    }


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
        "codec_tag_string": str(video_stream.get("codec_tag_string") or "").lower(),
        "video_codec_name": str(video_stream.get("codec_name") or "").lower(),
        "video_codec_tag_string": str(video_stream.get("codec_tag_string") or "").lower(),
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


def routing_exiftool_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    tags: dict[str, Any] = {}
    for key, value in payload.items():
        if key == "SourceFile":
            continue
        tag_key = normalize_tag_key(str(key).split(":")[-1])
        if tag_key:
            tags[tag_key] = normalize_metadata_value(value)
    return {
        "tags": tags,
        **{key: value for key, value in tags.items() if key in EXIFTOOL_FACT_KEYS},
    }


def exiftool_facts(summary: Mapping[str, Any]) -> dict[str, Any]:
    tags = summary.get("tags") if isinstance(summary.get("tags"), Mapping) else summary
    tag_map = cast(Mapping[str, Any], tags)

    def tag(name: str) -> Any:
        normalized = normalize_tag_key(name)
        if name in tag_map:
            return tag_map[name]
        if normalized in tag_map:
            return tag_map[normalized]
        return None

    width = parse_int(tag("image_width")) or 0
    height = parse_int(tag("image_height")) or 0
    return {
        "exif.file_name": str(tag("file_name") or ""),
        "exif.file_type_extension": lowercase_value(tag("file_type_extension")),
        "exif.mime_type": lowercase_value(tag("mime_type")),
        "exif.make": lowercase_value(tag("make")),
        "exif.model": str(tag("model") or "").strip(),
        "exif.model_lower": lowercase_value(tag("model")),
        "exif.software": lowercase_value(tag("software")),
        "exif.lens_model": lowercase_value(tag("lens_model")),
        "exif.camera_identifier": lowercase_value(tag("camera_identifier")),
        "exif.camera_direction": camera_direction(
            tag("camera_direction"),
            tag("lens_model"),
            tag("camera_identifier"),
        ),
        "exif.image_width": width,
        "exif.image_height": height,
        "exif.long_edge": max(width, height),
        "exif.short_edge": min(width, height) if width and height else 0,
        "exif.aspect_ratio": (max(width, height) / min(width, height)) if width and height else 0.0,
        "exif.orientation": lowercase_value(tag("orientation")),
        "exif.date_time_original": str(tag("date_time_original") or ""),
        "exif.create_date": str(tag("create_date") or ""),
        "exif.creation_date": str(tag("creation_date") or ""),
        "exif.burst_uuid": str(tag("burst_uuid") or ""),
        "exif.content_identifier": str(tag("content_identifier") or ""),
        "exif.still_image_time": str(tag("still_image_time") or ""),
        "exif.capture_mode": lowercase_value(tag("capture_mode")),
        "exif.full_frame_rate_playback_intent": lowercase_value(
            tag("full_frame_rate_playback_intent")
        ),
        "exif.auxiliary_image_type": lowercase_value(tag("auxiliary_image_type")),
        "exif.depth_map_image": boolish_value(tag("depth_map_image")),
        "exif.mp_image2": boolish_value(tag("mp_image2")),
        "exif.has_depth": boolish_value(tag("depth_map_image"))
        or boolish_value(tag("mp_image2"))
        or bool(tag("auxiliary_image_type")),
    }


EXIFTOOL_FACT_KEYS = {
    "file_name",
    "file_type_extension",
    "mime_type",
    "make",
    "model",
    "software",
    "lens_model",
    "camera_identifier",
    "camera_direction",
    "image_width",
    "image_height",
    "orientation",
    "date_time_original",
    "create_date",
    "creation_date",
    "burst_uuid",
    "content_identifier",
    "still_image_time",
    "capture_mode",
    "full_frame_rate_playback_intent",
    "auxiliary_image_type",
    "depth_map_image",
    "mp_image2",
}


def apply_pairing_rules(
    profile_routing: Mapping[str, Any],
    facts_by_path: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    out = {path: dict(facts) for path, facts in facts_by_path.items()}
    rules = profile_routing.get("pairings")
    if not isinstance(rules, list):
        return out
    for rule in rules:
        if not isinstance(rule, Mapping):
            continue
        rule_id = str(rule.get("id") or "").strip()
        if not rule_id:
            continue
        key = str(rule.get("key") or "exif.content_identifier").strip()
        still_when = mapping(rule.get("still"))
        movie_when = mapping(rule.get("movie"))
        stills = [
            path
            for path, facts in out.items()
            if predicate_matches(still_when, facts, profile_routing=profile_routing)
            and optional_fact(facts, key)
        ]
        movies = [
            path
            for path, facts in out.items()
            if predicate_matches(movie_when, facts, profile_routing=profile_routing)
            and optional_fact(facts, key)
        ]
        for still_path in stills:
            still_facts = out[still_path]
            pair_key = optional_fact(still_facts, key)
            if not pair_key:
                continue
            candidates = [path for path in movies if optional_fact(out[path], key) == pair_key]
            if not candidates:
                continue
            movie_path = choose_pair_movie(
                still_facts,
                candidates,
                out,
                bool(rule.get("prefer_same_stem")),
            )
            pairing_id = f"{rule_id}:{pair_key}"
            out[still_path] = {
                **out[still_path],
                "pair.kind": rule_id,
                "pair.id": pairing_id,
                "pair.role": "still",
                "pair.with": movie_path,
            }
            out[movie_path] = {
                **out[movie_path],
                "pair.kind": rule_id,
                "pair.id": pairing_id,
                "pair.role": "movie",
                "pair.with": still_path,
            }
    return out


def choose_pair_movie(
    still_facts: Mapping[str, Any],
    candidates: Sequence[str],
    facts_by_path: Mapping[str, Mapping[str, Any]],
    prefer_same_stem: bool,
) -> str:
    if prefer_same_stem:
        stem = optional_fact(still_facts, "path.stem_lower")
        for candidate in candidates:
            if optional_fact(facts_by_path[candidate], "path.stem_lower") == stem:
                return candidate
    return sorted(candidates)[0]


def route_matches(
    route: Mapping[str, Any],
    facts: Mapping[str, Any],
    *,
    profile_routing: Mapping[str, Any],
) -> bool:
    if set(route) - ROUTE_KEYS:
        return False
    when = route.get("when")
    if not isinstance(when, Mapping):
        return True
    return predicate_matches(when, facts, profile_routing=profile_routing)


def matched_fact_values(
    route: Mapping[str, Any],
    facts: Mapping[str, Any],
    *,
    profile_routing: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        name: fact_value(facts, name)
        for name in route_fact_names(route, profile_routing=profile_routing)
        if fact_value(facts, name) not in (None, "")
    }


def route_fact_names(
    route: Mapping[str, Any],
    *,
    profile_routing: Mapping[str, Any],
) -> list[str]:
    return sorted(
        predicate_fact_names(
            mapping(route.get("when")),
            profile_routing=profile_routing,
            seen_gates=set(),
        )
    )


def predicate_fact_names(
    predicate: Mapping[str, Any],
    *,
    profile_routing: Mapping[str, Any],
    seen_gates: set[str],
) -> set[str]:
    if not predicate:
        return set()
    names: set[str] = set()
    fact = predicate.get("fact")
    if isinstance(fact, str) and fact.strip():
        names.add(fact.strip())
    if predicate.get("pair") is not None or predicate.get("not_pair") is not None:
        names.add("pair.kind")
    if predicate.get("pair_role") is not None:
        names.add("pair.role")
    for key in ("all", "any"):
        items = predicate.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, Mapping):
                    names.update(
                        predicate_fact_names(
                            item,
                            profile_routing=profile_routing,
                            seen_gates=seen_gates,
                        )
                    )
    not_item = predicate.get("not")
    if isinstance(not_item, Mapping):
        names.update(
            predicate_fact_names(
                not_item,
                profile_routing=profile_routing,
                seen_gates=seen_gates,
            )
        )
    for gate_key in ("gate", "not_gate"):
        for gate_name in sequence(predicate.get(gate_key)):
            text = str(gate_name).strip()
            if not text or text in seen_gates:
                continue
            gate = mapping(mapping(profile_routing.get("gates")).get(text))
            if not gate:
                continue
            names.update(
                predicate_fact_names(
                    gate,
                    profile_routing=profile_routing,
                    seen_gates={*seen_gates, text},
                )
            )
    return names


def predicate_matches(
    predicate: Mapping[str, Any],
    facts: Mapping[str, Any],
    *,
    profile_routing: Mapping[str, Any],
) -> bool:
    if not predicate:
        return True
    if set(predicate) - PREDICATE_KEYS:
        return False
    all_items = predicate.get("all")
    if isinstance(all_items, list) and not all(
        isinstance(item, Mapping)
        and predicate_matches(item, facts, profile_routing=profile_routing)
        for item in all_items
    ):
        return False
    any_items = predicate.get("any")
    if isinstance(any_items, list) and not any(
        isinstance(item, Mapping)
        and predicate_matches(item, facts, profile_routing=profile_routing)
        for item in any_items
    ):
        return False
    not_item = predicate.get("not")
    if isinstance(not_item, Mapping) and predicate_matches(
        not_item,
        facts,
        profile_routing=profile_routing,
    ):
        return False
    if not gates_match(predicate.get("gate"), facts, profile_routing=profile_routing):
        return False
    not_gate = predicate.get("not_gate")
    if not_gate is not None and gates_match(not_gate, facts, profile_routing=profile_routing):
        return False
    path_predicate = predicate.get("path")
    if isinstance(path_predicate, Mapping) and not path_predicate_matches(path_predicate, facts):
        return False
    if "fact" in predicate and not single_fact_predicate_matches(predicate, facts):
        return False
    pair = predicate.get("pair")
    if pair is not None and not value_equals(fact_value(facts, "pair.kind"), pair):
        return False
    not_pair = predicate.get("not_pair")
    if not_pair is not None and value_equals(fact_value(facts, "pair.kind"), not_pair):
        return False
    pair_role = predicate.get("pair_role")
    if pair_role is not None and not value_equals(fact_value(facts, "pair.role"), pair_role):
        return False
    return True


def path_predicate_matches(predicate: Mapping[str, Any], facts: Mapping[str, Any]) -> bool:
    if set(predicate) - PATH_PREDICATE_KEYS:
        return False
    rel_path = str(fact_value(facts, "path.rel") or "")
    basename = str(fact_value(facts, "path.basename") or "")
    prefix = predicate.get("prefix")
    if isinstance(prefix, str) and prefix:
        normalized = normalize_posix_path(prefix).rstrip("/")
        if rel_path != normalized and not rel_path.startswith(f"{normalized}/"):
            return False
    glob = predicate.get("glob")
    if isinstance(glob, str) and glob and not fnmatch.fnmatchcase(rel_path, glob):
        return False
    filename_glob = predicate.get("filename_glob")
    if isinstance(filename_glob, str) and filename_glob and not fnmatch.fnmatchcase(
        basename,
        filename_glob,
    ):
        return False
    suffix = predicate.get("suffix")
    if suffix is not None and not value_equals(
        fact_value(facts, "path.suffix"),
        normalize_suffix(suffix),
    ):
        return False
    suffix_in = predicate.get("suffix_in")
    if suffix_in is not None and not value_in(
        fact_value(facts, "path.suffix"),
        [normalize_suffix(item) for item in sequence(suffix_in)],
    ):
        return False
    stem_regex = predicate.get("stem_regex")
    if stem_regex is not None and not re.search(
        str(stem_regex),
        str(fact_value(facts, "path.stem") or ""),
    ):
        return False
    basename_regex = predicate.get("basename_regex")
    if basename_regex is not None and not re.search(str(basename_regex), basename):
        return False
    return True


PREDICATE_KEYS = {
    "all",
    "any",
    "not",
    "gate",
    "not_gate",
    "path",
    "fact",
    "exists",
    "equals",
    "in",
    "contains",
    "regex",
    "min",
    "max",
    "between",
    "pair",
    "not_pair",
    "pair_role",
}


PATH_PREDICATE_KEYS = {
    "prefix",
    "glob",
    "filename_glob",
    "suffix",
    "suffix_in",
    "stem_regex",
    "basename_regex",
}


ROUTE_KEYS = {
    "id",
    "action",
    "group",
    "into",
    "when",
}


def single_fact_predicate_matches(predicate: Mapping[str, Any], facts: Mapping[str, Any]) -> bool:
    key = str(predicate.get("fact") or "").strip()
    if not key:
        return False
    actual = fact_value(facts, key)
    if "exists" in predicate:
        exists = actual not in (None, "")
        if bool(predicate.get("exists")) != exists:
            return False
    if "equals" in predicate and not value_equals(actual, predicate.get("equals")):
        return False
    if "in" in predicate and not value_in(actual, sequence(predicate.get("in"))):
        return False
    if "contains" in predicate and str(predicate.get("contains")).lower() not in str(
        actual or ""
    ).lower():
        return False
    if "regex" in predicate and not re.search(str(predicate.get("regex")), str(actual or "")):
        return False
    parsed = parse_float(actual)
    if "min" in predicate and (parsed is None or parsed < float(cast(float, predicate["min"]))):
        return False
    if "max" in predicate and (parsed is None or parsed > float(cast(float, predicate["max"]))):
        return False
    if "between" in predicate:
        bounds = list(sequence(predicate.get("between")))
        if len(bounds) != 2 or parsed is None:
            return False
        if parsed < float(cast(float, bounds[0])) or parsed > float(cast(float, bounds[1])):
            return False
    return True


def gates_match(
    gate_value: object,
    facts: Mapping[str, Any],
    *,
    profile_routing: Mapping[str, Any],
) -> bool:
    gates = profile_routing.get("gates")
    if not isinstance(gates, Mapping):
        return gate_value in (None, "", [], ())
    gate_names = [str(item) for item in sequence(gate_value) if str(item).strip()]
    if not gate_names:
        return True
    for name in gate_names:
        gate = gates.get(name)
        if not isinstance(gate, Mapping):
            return False
        if not predicate_matches(gate, facts, profile_routing=profile_routing):
            return False
    return True


def route_action(route: Mapping[str, Any]) -> RouteAction:
    action = str(route.get("action") or "upload").strip().lower()
    return "leave" if action == "leave" else "upload"


def route_collection_rel_path(route: Mapping[str, Any], rel_path: str) -> str:
    into = optional_normalized_dir(route.get("into"))
    if not into:
        return normalize_posix_path(rel_path)
    return normalize_posix_path(PurePosixPath(into, PurePosixPath(rel_path).name).as_posix())


def optional_normalized_dir(value: object) -> str | None:
    if value is None:
        return None
    text = normalize_posix_path(str(value).strip())
    if not text or text in {".", "/"}:
        return None
    if any(part in {"", ".", ".."} for part in text.split("/")):
        return None
    return text


def normalize_posix_path(value: str) -> str:
    text = str(value).strip().replace("\\", "/").lstrip("/")
    path = PurePosixPath(text)
    if any(part in {"", ".", ".."} for part in path.parts):
        return "/".join(part for part in path.parts if part not in {"", "."})
    return path.as_posix()


def routing_uses_fact_prefix(
    profile_routing: Mapping[str, Any],
    prefixes: tuple[str, ...],
) -> bool:
    for gate in mapping(profile_routing.get("gates")).values():
        if isinstance(gate, Mapping) and predicate_uses_fact_prefix(gate, prefixes):
            return True
    pairings = profile_routing.get("pairings")
    if isinstance(pairings, list):
        for rule in pairings:
            if not isinstance(rule, Mapping):
                continue
            if predicate_uses_fact_prefix(mapping(rule.get("still")), prefixes):
                return True
            if predicate_uses_fact_prefix(mapping(rule.get("movie")), prefixes):
                return True
            key = str(rule.get("key") or "exif.content_identifier")
            if key.startswith(prefixes):
                return True
    return any(
        predicate_uses_fact_prefix(mapping(route.get("when")), prefixes)
        for route in profile_routes(profile_routing)
    )


def predicate_uses_fact_prefix(
    predicate: Mapping[str, Any],
    prefixes: tuple[str, ...],
    *,
    profile_routing: Mapping[str, Any] | None = None,
    seen_gates: set[str] | None = None,
) -> bool:
    if not predicate:
        return False
    seen = seen_gates or set()
    fact = predicate.get("fact")
    if isinstance(fact, str) and fact.startswith(prefixes):
        return True
    for key in ("all", "any"):
        items = predicate.get(key)
        if isinstance(items, list) and any(
            isinstance(item, Mapping)
            and predicate_uses_fact_prefix(
                item,
                prefixes,
                profile_routing=profile_routing,
                seen_gates=seen,
            )
            for item in items
        ):
            return True
    not_item = predicate.get("not")
    if isinstance(not_item, Mapping) and predicate_uses_fact_prefix(
        not_item,
        prefixes,
        profile_routing=profile_routing,
        seen_gates=seen,
    ):
        return True
    if profile_routing is None:
        return False
    gates = mapping(profile_routing.get("gates"))
    for key in ("gate", "not_gate"):
        for gate_name in sequence(predicate.get(key)):
            text = str(gate_name).strip()
            if not text or text in seen:
                continue
            gate = gates.get(text)
            if isinstance(gate, Mapping) and predicate_uses_fact_prefix(
                gate,
                prefixes,
                profile_routing=profile_routing,
                seen_gates={*seen, text},
            ):
                return True
    return False


def lower_mapping(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key).strip().lower(): str(item).strip().lower()
        for key, item in value.items()
        if str(key).strip() and str(item).strip()
    }


def mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def sequence(value: object) -> Sequence[Any]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence):
        return value
    return (value,)


def flatten_facts(value: Mapping[str, Any], *, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, item in value.items():
        text_key = str(key)
        full_key = f"{prefix}.{text_key}" if prefix else text_key
        if isinstance(item, Mapping):
            out[text_key if not prefix else full_key] = item
            out.update(flatten_facts(item, prefix=full_key))
        else:
            out[full_key] = item
    return out


def fact_value(facts: Mapping[str, Any], key: str) -> Any:
    if key in facts:
        return facts[key]
    current: Any = facts
    for part in key.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def optional_fact(facts: Mapping[str, Any], key: str) -> str | None:
    value = fact_value(facts, key)
    if value in (None, ""):
        return None
    return str(value)


def value_equals(actual: Any, expected: Any) -> bool:
    if isinstance(actual, Sequence) and not isinstance(actual, str):
        return any(value_equals(item, expected) for item in actual)
    if isinstance(expected, bool):
        return bool(actual) == expected
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        parsed = parse_float(actual)
        return parsed is not None and abs(parsed - float(expected)) < 0.01
    return str(actual or "").lower() == str(expected or "").lower()


def value_in(actual: Any, expected_values: Sequence[Any]) -> bool:
    return any(value_equals(actual, expected) for expected in expected_values)


def normalize_suffix(value: object) -> str:
    suffix = str(value or "").strip().lower()
    if suffix and not suffix.startswith("."):
        suffix = f".{suffix}"
    return suffix


def normalize_tag_key(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    text = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", text)
    return text.lower()


def normalize_metadata_value(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip()
    return value


def lowercase_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def boolish_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text not in {"", "0", "false", "no", "none", "n/a"}


def camera_direction(*values: Any) -> str:
    text = " ".join(str(value or "").lower() for value in values)
    if "front" in text or "selfie" in text:
        return "front"
    if "back" in text or "rear" in text or "wide" in text or "tele" in text:
        return "rear"
    return ""


def video_resolution_label(width: int, height: int) -> str:
    edge = (max(width, height), min(width, height))
    if edge == (3840, 2160):
        return "4k"
    if edge == (1920, 1080):
        return "1080p"
    if edge == (1280, 720):
        return "720p"
    if width and height:
        return f"{edge[0]}x{edge[1]}"
    return ""


def video_codec_label(codec_name: str, codec_tag: str = "") -> str:
    text = f"{codec_name} {codec_tag}".lower()
    if "hevc" in text or "hvc1" in text:
        return "hevc"
    if "h264" in text or "avc1" in text:
        return "h264"
    return codec_name.lower()


def parse_rate(value: object) -> float | None:
    if value in (None, "", "0/0", "N/A"):
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

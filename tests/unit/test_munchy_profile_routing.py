from __future__ import annotations

from munchy.profile_routing import (
    ProfileRoutingFile,
    match_profile_route,
    profile_routing_plan,
    routing_exiftool_summary,
    routing_file_facts,
)


def test_profile_routing_uses_ordered_predicates_and_output_directory() -> None:
    routing = {
        "routes": [
            {
                "id": "iphone-native-video",
                "group": "video",
                "into": "iphone-se2/video",
                "when": {
                    "all": [
                        {"path": {"prefix": "nash-iphone-se2", "suffix": ".mov"}},
                        {"fact": "path.stem", "regex": "^IMG_[0-9]{4}$"},
                    ]
                },
            },
            {
                "id": "iphone-library-review",
                "group": "library-review",
                "into": "iphone-se2/downloads",
                "when": {"path": {"prefix": "nash-iphone-se2"}},
            },
        ]
    }

    native = match_profile_route(routing, "nash-iphone-se2/IMG_0001.MOV")
    saved = match_profile_route(routing, "nash-iphone-se2/imported/clip.mp4")
    other_source = match_profile_route(routing, "katie-pixel-8/imported/clip.mp4")

    assert native is not None
    assert native.route_id == "iphone-native-video"
    assert native.group == "video"
    assert native.collection_rel_path == "iphone-se2/video/IMG_0001.MOV"
    assert saved is not None
    assert saved.route_id == "iphone-library-review"
    assert saved.group == "library-review"
    assert saved.collection_rel_path == "iphone-se2/downloads/clip.mp4"
    assert other_source is None


def test_profile_routing_can_intentionally_order_broad_review_first() -> None:
    routing = {
        "routes": [
            {
                "id": "phone-library-review",
                "group": "library-review",
                "when": {"path": {"prefix": "phone"}},
            },
            {
                "id": "phone-video",
                "group": "video",
                "when": {"path": {"prefix": "phone", "suffix": ".mov"}},
            },
        ]
    }

    match = match_profile_route(routing, "phone/IMG_0001.MOV")

    assert match is not None
    assert match.route_id == "phone-library-review"
    assert match.group == "library-review"


def test_profile_routing_uses_ffprobe_facts_before_falling_through() -> None:
    calls = 0

    def load_probe() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "codec_name": "hevc",
            "width": 1920,
            "height": 1080,
            "fps": 60.0,
            "format_tags": {},
            "stream_tags": {},
        }

    routing = {
        "routes": [
            {
                "id": "iphone-h264-video",
                "group": "h264-video",
                "when": {
                    "all": [
                        {"path": {"prefix": "phone", "suffix": ".mov"}},
                        {"fact": "video.codec", "equals": "h264"},
                    ]
                },
            },
            {
                "id": "iphone-video-review",
                "group": "video-review",
                "when": {"path": {"prefix": "phone"}},
            },
        ]
    }

    match = match_profile_route(
        routing,
        "phone/IMG_0001.MOV",
        probe_summary_loader=load_probe,
    )

    assert calls == 1
    assert match is not None
    assert match.route_id == "iphone-video-review"


def test_profile_route_metadata_need_detection_traverses_gates() -> None:
    calls = 0

    def load_facts() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return routing_file_facts(
            "phone/IMG_0001.HEIC",
            exiftool_summary={"tags": {"make": "Apple", "model": "iPhone SE (2nd generation)"}},
        )

    routing = {
        "gates": {
            "iphone-native": {
                "all": [
                    {"fact": "exif.make", "equals": "apple"},
                    {"fact": "exif.model", "equals": "iPhone SE (2nd generation)"},
                ]
            }
        },
        "routes": [
            {
                "id": "iphone-photo",
                "group": "photos",
                "when": {
                    "all": [
                        {"gate": "iphone-native"},
                        {"path": {"suffix": ".heic"}},
                    ]
                },
            }
        ],
    }

    match = match_profile_route(
        routing,
        "phone/IMG_0001.HEIC",
        routing_facts_loader=load_facts,
    )

    assert calls == 1
    assert match is not None
    assert match.route_id == "iphone-photo"


def test_profile_routing_pairs_live_photo_before_video_fallback() -> None:
    routing = {
        "pairings": [
            {
                "id": "live_photo",
                "key": "exif.content_identifier",
                "prefer_same_stem": True,
                "still": {
                    "all": [
                        {"path": {"suffix_in": [".heic", ".jpg", ".jpeg"]}},
                        {"fact": "exif.content_identifier", "exists": True},
                    ]
                },
                "movie": {
                    "all": [
                        {"path": {"suffix": ".mov"}},
                        {"fact": "exif.content_identifier", "exists": True},
                    ]
                },
            }
        ],
        "routes": [
            {
                "id": "live-photo",
                "group": "live-photo",
                "into": "live-photo",
                "when": {"pair": "live_photo"},
            },
            {
                "id": "video",
                "group": "video",
                "when": {"path": {"suffix": ".mov"}},
            },
        ],
    }
    files = [
        ProfileRoutingFile(
            path="phone/IMG_0001.HEIC",
            bytes=10,
            routing_facts=routing_file_facts(
                "phone/IMG_0001.HEIC",
                exiftool_summary={"tags": {"content_identifier": "pair-1"}},
            ),
        ),
        ProfileRoutingFile(
            path="phone/IMG_0001.MOV",
            bytes=20,
            routing_facts=routing_file_facts(
                "phone/IMG_0001.MOV",
                exiftool_summary={"tags": {"content_identifier": "pair-1"}},
            ),
        ),
    ]

    plan = profile_routing_plan(routing, files, group_names={"live-photo", "video"})

    assert plan.ok is True
    assert [item["group"] for item in plan.matches] == ["live-photo", "live-photo"]
    assert [item["pair_kind"] for item in plan.matches] == ["live_photo", "live_photo"]
    assert [item["pair_role"] for item in plan.matches] == ["still", "movie"]
    assert [item["collection_rel_path"] for item in plan.matches] == [
        "live-photo/IMG_0001.HEIC",
        "live-photo/IMG_0001.MOV",
    ]


def test_profile_routing_attaches_xmp_sidecar_as_evidence() -> None:
    routing = {
        "sidecars": [
            {
                "id": "xmp",
                "format": "xmp",
                "primary": {"path": {"suffix": ".mov"}},
            }
        ],
        "routes": [
            {
                "id": "video",
                "group": "video",
                "when": {"path": {"suffix": ".mov"}},
            },
        ],
    }

    plan = profile_routing_plan(
        routing,
        [
            ProfileRoutingFile(path="phone/IMG_0001.MOV", bytes=100),
            ProfileRoutingFile(path="phone/IMG_0001.MOV.xmp", bytes=20),
        ],
        group_names={"video"},
    )

    assert plan.ok is True
    assert plan.unmatched == []
    assert [item["action"] for item in plan.matches] == ["upload", "evidence"]
    assert plan.matches[0]["path"] == "phone/IMG_0001.MOV"
    assert plan.matches[0]["group"] == "video"
    assert plan.matches[1]["path"] == "phone/IMG_0001.MOV.xmp"
    assert plan.matches[1]["route_id"] == "xmp"
    assert plan.matches[1]["group"] == "video"
    assert plan.matches[1]["sidecar_id"] == "xmp"
    assert plan.matches[1]["sidecar_format"] == "xmp"
    assert plan.matches[1]["sidecar_for"] == "phone/IMG_0001.MOV"


def test_profile_routing_leaves_sidecar_evidence_for_left_primary() -> None:
    routing = {
        "sidecars": [
            {
                "id": "xmp",
                "format": "xmp",
                "primary": {"path": {"suffix": ".mov"}},
            }
        ],
        "routes": [
            {
                "id": "leave-video",
                "action": "leave",
                "when": {"path": {"suffix": ".mov"}},
            },
        ],
    }

    plan = profile_routing_plan(
        routing,
        [
            ProfileRoutingFile(path="phone/IMG_0001.MOV", bytes=100),
            ProfileRoutingFile(path="phone/IMG_0001.MOV.xmp", bytes=20),
        ],
        group_names={"video"},
    )

    assert plan.ok is True
    assert plan.left_files == 2
    assert [item["path"] for item in plan.left] == [
        "phone/IMG_0001.MOV",
        "phone/IMG_0001.MOV.xmp",
    ]
    assert plan.left[1]["action"] == "evidence"
    assert plan.left[1]["reason"] == "sidecar_for_left_primary"
    assert plan.left[1]["sidecar_for"] == "phone/IMG_0001.MOV"
    assert plan.unmatched == []


def test_profile_routing_rejects_sidecar_evidence_for_unmatched_primary() -> None:
    routing = {
        "sidecars": [
            {
                "id": "xmp",
                "format": "xmp",
                "primary": {"path": {"suffix": ".mov"}},
            }
        ],
        "routes": [],
    }

    plan = profile_routing_plan(
        routing,
        [
            ProfileRoutingFile(path="phone/IMG_0001.MOV", bytes=100),
            ProfileRoutingFile(path="phone/IMG_0001.MOV.xmp", bytes=20),
        ],
        group_names={"video"},
    )

    assert plan.ok is False
    assert plan.left_files == 0
    assert plan.unmatched == [
        {
            "path": "phone/IMG_0001.MOV",
            "bytes": 100,
            "reason": "no_matching_route",
            "probe_error": None,
            "facts_error": None,
        },
        {
            "path": "phone/IMG_0001.MOV.xmp",
            "bytes": 20,
            "reason": "orphan_sidecar_evidence",
            "sidecar_for": "phone/IMG_0001.MOV",
            "probe_error": None,
            "facts_error": None,
        }
    ]


def test_profile_routing_leave_action_excludes_intentional_leftovers() -> None:
    routing = {
        "routes": [
            {
                "id": "known-video",
                "group": "video",
                "when": {"path": {"prefix": "phone", "suffix": ".mov"}},
            },
            {
                "id": "downloads",
                "action": "leave",
                "when": {"path": {"prefix": "phone/downloads"}},
            },
        ]
    }

    plan = profile_routing_plan(
        routing,
        [
            ProfileRoutingFile(path="phone/IMG_0001.MOV", bytes=100),
            ProfileRoutingFile(path="phone/downloads/sent-by-someone.mp4", bytes=200),
        ],
        group_names={"video"},
    )

    assert plan.ok is True
    assert [item["path"] for item in plan.matches] == ["phone/IMG_0001.MOV"]
    assert plan.left == [
        {
            "path": "phone/downloads/sent-by-someone.mp4",
            "bytes": 200,
            "route_id": "downloads",
            "route_index": 1,
            "action": "leave",
            "pair_kind": None,
            "pairing_id": None,
            "pair_role": None,
            "pair_with": None,
            "matched_facts": {},
        }
    ]


def test_routing_exiftool_summary_normalizes_apple_tags() -> None:
    facts = routing_file_facts(
        "phone/IMG_0001.HEIC",
        exiftool_summary=routing_exiftool_summary(
            {
                "File:FileTypeExtension": "HEIC",
                "EXIF:Make": "Apple",
                "EXIF:Model": "iPhone SE (2nd generation)",
                "Composite:LensModel": "iPhone SE (2nd generation) back camera 3.99mm f/1.8",
                "QuickTime:ContentIdentifier": "asset-id",
                "Keys:FullFrameRatePlaybackIntent": 0,
                "Composite:GPSLatitude": "48.99951389 N",
                "Composite:GPSLongitude": "122.74040278 W",
            }
        ),
    )

    assert facts["exif.file_type_extension"] == "heic"
    assert facts["exif.make"] == "apple"
    assert facts["exif.model_lower"] == "iphone se (2nd generation)"
    assert facts["exif.camera_direction"] == "rear"
    assert facts["exif.content_identifier"] == "asset-id"
    assert facts["exif.full_frame_rate_playback_intent"] == "0"
    assert facts["exif.gps_latitude"] == "48.99951389 N"
    assert facts["exif.gps_longitude"] == "122.74040278 W"

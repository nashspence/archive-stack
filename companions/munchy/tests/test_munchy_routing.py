from __future__ import annotations

import pytest
from munchy_api_client.routing import (
    RoutingFile,
    apply_sidecar_rules,
    exiftool_routing_facts,
    match_route,
    routing_exiftool_summary,
    routing_exiftool_tags,
    routing_file_facts,
    routing_file_requires_exiftool,
    routing_file_requires_probe,
    routing_plan,
    sidecar_exiftool_fact_requests,
)


def test_routing_uses_ordered_predicates_and_output_directory() -> None:
    routing = {
        "routes": [
            {
                "id": "iphone-native-video",
                "group": "video",
                "into": "iphone-se2/video",
                "when": {
                    "all": [
                        {"path": {"prefix": "example-phone", "suffix": ".mov"}},
                        {"fact": "path.stem", "regex": "^IMG_[0-9]{4}$"},
                    ]
                },
            },
            {
                "id": "iphone-library-review",
                "group": "library-review",
                "into": "iphone-se2/downloads",
                "when": {"path": {"prefix": "example-phone"}},
            },
        ]
    }

    native = match_route(routing, "example-phone/IMG_0001.MOV")
    saved = match_route(routing, "example-phone/imported/clip.mp4")
    other_source = match_route(routing, "example-other-phone/imported/clip.mp4")

    assert native is not None
    assert native.route_id == "iphone-native-video"
    assert native.group == "video"
    assert native.collection_rel_path == "iphone-se2/video/IMG_0001.MOV"
    assert saved is not None
    assert saved.route_id == "iphone-library-review"
    assert saved.group == "library-review"
    assert saved.collection_rel_path == "iphone-se2/downloads/clip.mp4"
    assert other_source is None


def test_routing_can_intentionally_order_broad_review_first() -> None:
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

    match = match_route(routing, "phone/IMG_0001.MOV")

    assert match is not None
    assert match.route_id == "phone-library-review"
    assert match.group == "library-review"


def test_routing_uses_ffprobe_facts_before_falling_through() -> None:
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

    match = match_route(
        routing,
        "phone/IMG_0001.MOV",
        probe_summary_loader=load_probe,
    )

    assert calls == 1
    assert match is not None
    assert match.route_id == "iphone-video-review"


def test_routing_exposes_audio_probe_rates_used_by_device_profiles() -> None:
    facts = routing_file_facts(
        "recorder-audio-0001.wav",
        probe_summary={
            "has_audio": True,
            "audio_codec_name": "pcm_s16le",
            "audio_channels": 1,
            "audio_sample_rate": 44_100,
            "audio_bit_rate": 705_600,
        },
    )
    routing = {
        "routes": [
            {
                "id": "pcm-wav",
                "group": "audio",
                "when": {
                    "all": [
                        {"fact": "audio.codec_name", "equals": "pcm_s16le"},
                        {"fact": "ffprobe.audio_sample_rate", "between": [44_100, 44_130]},
                        {"fact": "ffprobe.audio_bit_rate", "equals": 705_600},
                    ]
                },
            }
        ]
    }

    match = match_route(
        routing,
        "recorder-audio-0001.wav",
        routing_facts=facts,
    )

    assert facts["ffprobe.audio_sample_rate"] == 44_100
    assert facts["ffprobe.audio_bit_rate"] == 705_600
    assert match is not None
    assert match.route_id == "pcm-wav"


def test_routing_skips_expensive_facts_for_prior_path_only_route() -> None:
    routing = {
        "routes": [
            {
                "id": "device-state",
                "group": "evidence",
                "when": {"path": {"filename_glob": "leinfo.sav"}},
            },
            {
                "id": "camera-video",
                "group": "video",
                "when": {
                    "all": [
                        {"path": {"suffix": ".mp4"}},
                        {"fact": "video.codec", "equals": "hevc"},
                        {"fact": "exif.make", "equals": "example imaging"},
                    ]
                },
            },
        ]
    }
    state_facts = routing_file_facts("camera/leinfo.sav")
    video_facts = routing_file_facts("camera/GX010001.MP4")

    assert routing_file_requires_probe(routing, state_facts) is False
    assert routing_file_requires_exiftool(routing, state_facts) is False
    assert routing_file_requires_probe(routing, video_facts) is True
    assert (
        routing_file_requires_exiftool(
            routing,
            routing_file_facts(
                "camera/GX010001.MP4",
                probe_summary={"codec_name": "hevc", "has_video": True},
            ),
        )
        is True
    )


def test_routing_lazy_match_does_not_probe_path_excluded_route() -> None:
    calls = 0

    def load_probe() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"codec_name": "hevc", "has_video": True}

    routing = {
        "routes": [
            {
                "id": "camera-video",
                "group": "video",
                "when": {
                    "all": [
                        {"path": {"suffix": ".mp4"}},
                        {"fact": "video.codec", "equals": "hevc"},
                    ]
                },
            },
            {
                "id": "device-state",
                "group": "evidence",
                "when": {"path": {"filename_glob": "leinfo.sav"}},
            },
        ]
    }

    match = match_route(
        routing,
        "camera/leinfo.sav",
        probe_summary_loader=load_probe,
    )

    assert calls == 0
    assert match is not None
    assert match.route_id == "device-state"


def test_route_metadata_need_detection_traverses_gates() -> None:
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

    match = match_route(
        routing,
        "phone/IMG_0001.HEIC",
        routing_facts_loader=load_facts,
    )

    assert calls == 1
    assert match is not None
    assert match.route_id == "iphone-photo"


def test_routing_pairs_live_photo_before_video_fallback() -> None:
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
        RoutingFile(
            path="phone/IMG_0001.HEIC",
            bytes=10,
            routing_facts=routing_file_facts(
                "phone/IMG_0001.HEIC",
                exiftool_summary={"tags": {"content_identifier": "pair-1"}},
            ),
        ),
        RoutingFile(
            path="phone/IMG_0001.MOV",
            bytes=20,
            routing_facts=routing_file_facts(
                "phone/IMG_0001.MOV",
                exiftool_summary={"tags": {"content_identifier": "pair-1"}},
            ),
        ),
    ]

    plan = routing_plan(routing, files, group_names={"live-photo", "video"})

    assert plan.ok is True
    assert [item["group"] for item in plan.matches] == ["live-photo", "live-photo"]
    assert [item["pair_kind"] for item in plan.matches] == ["live_photo", "live_photo"]
    assert [item["pair_role"] for item in plan.matches] == ["still", "movie"]
    assert [item["collection_rel_path"] for item in plan.matches] == [
        "live-photo/IMG_0001.HEIC",
        "live-photo/IMG_0001.MOV",
    ]


def test_routing_attaches_xmp_sidecar_as_evidence() -> None:
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

    plan = routing_plan(
        routing,
        [
            RoutingFile(path="phone/IMG_0001.MOV", bytes=100),
            RoutingFile(path="phone/IMG_0001.MOV.xmp", bytes=20),
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


def test_routing_can_match_primary_from_generic_sidecar_facts() -> None:
    routing = {
        "sidecars": [
            {
                "id": "camera_xml",
                "format": "xml",
                "path": "{parent}/{stem}M01.XML",
                "primary": {"path": {"suffix": ".mp4"}},
                "facts": {
                    "source": "exiftool",
                    "tags": ["Make", "Model", "CaptureFPS", "CodecProfile"],
                },
            }
        ],
        "routes": [
            {
                "id": "sidecar-camera-video",
                "group": "video",
                "when": {
                    "all": [
                        {"path": {"suffix": ".mp4"}},
                        {
                            "fact": "sidecars.camera_xml.facts.exif.make",
                            "equals": "example imaging",
                        },
                        {
                            "fact": "sidecars.camera_xml.facts.exif.model_lower",
                            "equals": "synthetic camera",
                        },
                        {
                            "fact": "sidecars.camera_xml.facts.exiftool.tags.capture_fps",
                            "equals": "119.88",
                        },
                    ]
                },
            }
        ],
    }

    sidecar_facts = exiftool_routing_facts(
        routing_exiftool_summary(
            {
                "EXIF:Make": "Example Imaging",
                "EXIF:Model": "Synthetic Camera",
                "XMP:CaptureFPS": "119.88",
                "XMP:CodecProfile": "Example 4K",
            }
        )
    )
    plan = routing_plan(
        routing,
        [
            RoutingFile(path="camera/C0001.MP4", bytes=100),
            RoutingFile(
                path="camera/C0001M01.XML",
                bytes=20,
                sidecar_facts=sidecar_facts,
            ),
        ],
        group_names={"video"},
    )

    assert plan.ok is True
    assert [item["action"] for item in plan.matches] == ["upload", "evidence"]
    assert plan.matches[0]["route_id"] == "sidecar-camera-video"
    assert plan.matches[0]["matched_facts"] == {
        "sidecars.camera_xml.facts.exif.make": "example imaging",
        "sidecars.camera_xml.facts.exif.model_lower": "synthetic camera",
        "sidecars.camera_xml.facts.exiftool.tags.capture_fps": "119.88",
    }
    assert plan.matches[1]["sidecar_id"] == "camera_xml"
    assert plan.matches[1]["sidecar_for"] == "camera/C0001.MP4"


def test_sidecar_facts_can_unlock_primary_exiftool_collection() -> None:
    routing = {
        "extra_exiftool_tags": ["VideoAvgBitrate"],
        "sidecars": [
            {
                "id": "camera_xml",
                "format": "xml",
                "path": "{parent}/{stem}M01.XML",
                "primary": {"path": {"suffix": ".mp4"}},
                "facts": {"source": "exiftool", "tags": ["Make"]},
            }
        ],
        "routes": [
            {
                "id": "sidecar-and-bitrate-video",
                "group": "video",
                "when": {
                    "all": [
                        {"path": {"suffix": ".mp4"}},
                        {
                            "fact": "sidecars.camera_xml.facts.exif.make",
                            "equals": "example imaging",
                        },
                        {
                            "fact": "exiftool.tags.video_avg_bitrate",
                            "equals": "200 Mbps",
                        },
                    ]
                },
            }
        ],
    }
    path_facts_by_path = {
        "camera/C0001.MP4": routing_file_facts("camera/C0001.MP4"),
        "camera/C0001M01.XML": routing_file_facts("camera/C0001M01.XML"),
    }
    sidecar_facts = {
        "camera/C0001M01.XML": exiftool_routing_facts(
            routing_exiftool_summary({"EXIF:Make": "Example Imaging"})
        )
    }

    base_facts_by_path = apply_sidecar_rules(
        routing,
        path_facts_by_path,
        sidecar_facts_by_path=sidecar_facts,
        require_configured_facts=False,
    )

    assert routing_file_requires_exiftool(
        routing,
        base_facts_by_path["camera/C0001.MP4"],
    )


def test_sidecar_fact_requests_include_configured_extractors() -> None:
    routing = {
        "sidecars": [
            {
                "id": "camera_xml",
                "format": "xml",
                "path": "{parent}/{stem}.xml",
                "primary": {"path": {"suffix": ".mp4"}},
                "facts": {
                    "source": "exiftool",
                    "tags": ["VendorGroup", "VendorFieldName", "VendorFieldValue"],
                    "extractors": [
                        {
                            "type": "name_value",
                            "name_tag": "VendorFieldName",
                            "value_tag": "VendorFieldValue",
                            "requires": [{"tag": "VendorGroup", "contains": "Gps"}],
                            "fields": {"Latitude": "exif.gps_latitude"},
                        }
                    ],
                },
            }
        ],
        "routes": [],
    }
    facts_by_path = {
        "camera/C0001.MP4": routing_file_facts("camera/C0001.MP4"),
        "camera/C0001.xml": routing_file_facts("camera/C0001.xml"),
    }

    requests = sidecar_exiftool_fact_requests(routing, facts_by_path)

    assert requests["camera/C0001.xml"].tags == (
        "VendorGroup",
        "VendorFieldName",
        "VendorFieldValue",
    )
    assert requests["camera/C0001.xml"].fact_extractors == (
        {
            "type": "name_value",
            "name_tag": "vendor_field_name",
            "value_tag": "vendor_field_value",
            "requires": ({"tag": "vendor_group", "contains": "Gps"},),
            "fields": {"latitude": "exif.gps_latitude"},
        },
    )


def test_sidecar_fact_extractors_must_reference_requested_tags() -> None:
    routing = {
        "sidecars": [
            {
                "id": "camera_xml",
                "format": "xml",
                "path": "{parent}/{stem}.xml",
                "primary": {"path": {"suffix": ".mp4"}},
                "facts": {
                    "source": "exiftool",
                    "tags": ["VendorFieldName", "VendorFieldValue"],
                    "extractors": [
                        {
                            "type": "name_value",
                            "name_tag": "VendorFieldName",
                            "value_tag": "VendorFieldValue",
                            "requires": [{"tag": "VendorGroup", "contains": "Gps"}],
                            "fields": {"Latitude": "exif.gps_latitude"},
                        }
                    ],
                },
            }
        ],
        "routes": [],
    }
    facts_by_path = {
        "camera/C0001.MP4": routing_file_facts("camera/C0001.MP4"),
        "camera/C0001.xml": routing_file_facts("camera/C0001.xml"),
    }

    with pytest.raises(ValueError, match="vendor_group"):
        sidecar_exiftool_fact_requests(routing, facts_by_path)


def test_routing_fails_when_configured_sidecar_facts_are_missing() -> None:
    routing = {
        "sidecars": [
            {
                "id": "camera_xml",
                "format": "xml",
                "path": "{parent}/{stem}M01.XML",
                "primary": {"path": {"suffix": ".mp4"}},
                "facts": {"source": "exiftool", "tags": ["Make"]},
            }
        ],
        "routes": [
            {
                "id": "sidecar-camera-video",
                "group": "video",
                "when": {
                    "fact": "sidecars.camera_xml.facts.exif.make",
                    "equals": "example imaging",
                },
            },
            {
                "id": "broad-video-fallback",
                "group": "video",
                "when": {"path": {"suffix": ".mp4"}},
            },
        ],
    }

    plan = routing_plan(
        routing,
        [RoutingFile(path="camera/C0001.MP4", bytes=100)],
        group_names={"video"},
    )

    assert plan.ok is False
    assert plan.matches == []
    assert plan.unmatched[0]["path"] == "camera/C0001.MP4"
    assert plan.unmatched[0]["reason"] == "sidecar_facts_failed"
    assert (
        "camera_xml: configured sidecar facts source not found"
        in (plan.unmatched[0]["sidecar_facts_error"])
    )


def test_routing_fails_when_configured_sidecar_facts_are_unparsable() -> None:
    routing = {
        "sidecars": [
            {
                "id": "camera_xml",
                "format": "xml",
                "path": "{parent}/{stem}M01.XML",
                "primary": {"path": {"suffix": ".mp4"}},
                "facts": {"source": "exiftool", "tags": ["Make"]},
            }
        ],
        "routes": [
            {
                "id": "sidecar-camera-video",
                "group": "video",
                "when": {
                    "fact": "sidecars.camera_xml.facts.exif.make",
                    "equals": "example imaging",
                },
            },
        ],
    }

    plan = routing_plan(
        routing,
        [
            RoutingFile(path="camera/C0001.MP4", bytes=100),
            RoutingFile(
                path="camera/C0001M01.XML",
                bytes=20,
                sidecar_facts_error="exiftool returned invalid JSON",
            ),
        ],
        group_names={"video"},
    )

    assert plan.ok is False
    assert plan.unmatched[0]["path"] == "camera/C0001.MP4"
    assert plan.unmatched[0]["reason"] == "sidecar_facts_failed"
    assert (
        "camera/C0001M01.XML: exiftool returned invalid JSON"
        in (plan.unmatched[0]["sidecar_facts_error"])
    )


def test_routing_leaves_sidecar_evidence_for_left_primary() -> None:
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

    plan = routing_plan(
        routing,
        [
            RoutingFile(path="phone/IMG_0001.MOV", bytes=100),
            RoutingFile(path="phone/IMG_0001.MOV.xmp", bytes=20),
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


def test_routing_rejects_sidecar_evidence_for_unmatched_primary() -> None:
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

    plan = routing_plan(
        routing,
        [
            RoutingFile(path="phone/IMG_0001.MOV", bytes=100),
            RoutingFile(path="phone/IMG_0001.MOV.xmp", bytes=20),
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
        },
    ]


def test_routing_leave_action_excludes_intentional_leftovers() -> None:
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

    plan = routing_plan(
        routing,
        [
            RoutingFile(path="phone/IMG_0001.MOV", bytes=100),
            RoutingFile(path="phone/downloads/sent-by-someone.mp4", bytes=200),
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


def test_routing_exiftool_tags_adds_explicit_vendor_tags_once() -> None:
    tags = routing_exiftool_tags(
        {
            "extra_exiftool_tags": [
                "AndroidCaptureFPS",
                "-SpecialTypeID",
                "AndroidCaptureFPS",
            ]
        }
    )

    assert "FileName" in tags
    assert tags.count("AndroidCaptureFPS") == 1
    assert "SpecialTypeID" in tags


def test_routing_exiftool_tags_rejects_shell_like_tags() -> None:
    try:
        routing_exiftool_tags({"extra_exiftool_tags": ["Make;rm"]})
    except ValueError as exc:
        assert "invalid ExifTool routing tag" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("expected invalid ExifTool tag to be rejected")


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


def test_routing_exiftool_summary_exposes_generic_camera_fields() -> None:
    facts = routing_file_facts(
        "phone/PXL_0001.JPG",
        exiftool_summary=routing_exiftool_summary(
            {
                "Composite:LensID": "Pixel 8 front camera 2.74mm f/2.2",
                "MakerNotes:CameraType": "Selfie",
                "Composite:Rotation": 180,
            }
        ),
    )

    assert facts["exif.lens_id"] == "pixel 8 front camera 2.74mm f/2.2"
    assert facts["exif.camera_type"] == "selfie"
    assert facts["exif.camera_direction"] == "front"
    assert facts["exif.rotation"] == 180


def test_routing_exiftool_summary_preserves_grouped_tag_facts() -> None:
    summary = routing_exiftool_summary(
        {
            "QuickTime:ChromaFormat": "4:2:2",
            "QuickTime:Copy1:ChromaFormat": "4:2:2",
            "QuickTime:Copy2:ChromaFormat": "4:2:0",
        }
    )
    facts = routing_file_facts(
        "camera/DSC0001.HIF",
        routing_facts=exiftool_routing_facts(summary),
    )

    assert summary["tags"]["chroma_format"] == ["4:2:2", "4:2:2", "4:2:0"]
    assert summary["tags"]["quick_time_chroma_format"] == "4:2:2"
    assert facts["exiftool.tags.chroma_format"] == ["4:2:2", "4:2:2", "4:2:0"]
    assert facts["exiftool.tags.quick_time_chroma_format"] == "4:2:2"
    assert facts["exiftool.tags.quick_time_copy1_chroma_format"] == "4:2:2"
    assert facts["exiftool.tags.quick_time_copy2_chroma_format"] == "4:2:0"


def test_routing_exiftool_camera_direction_prefers_lens_over_camera_type() -> None:
    rear_facts = routing_file_facts(
        "phone/IMG_0001.JPG",
        exiftool_summary=routing_exiftool_summary(
            {
                "ExifIFD:LensModel": "iPhone SE back camera 3.99mm f/1.8",
                "Apple:CameraType": "Front",
            }
        ),
    )
    front_facts = routing_file_facts(
        "phone/IMG_0002.JPG",
        exiftool_summary=routing_exiftool_summary(
            {
                "ExifIFD:LensModel": "iPhone SE front camera 2.87mm f/2.2",
                "Apple:CameraType": "Back Wide Angle",
            }
        ),
    )

    assert rear_facts["exif.camera_direction"] == "rear"
    assert front_facts["exif.camera_direction"] == "front"


def test_exiftool_routing_facts_applies_configured_name_value_extractor() -> None:
    facts = exiftool_routing_facts(
        routing_exiftool_summary(
            {
                "VendorMetaGroupName": [
                    "GpsGroup",
                    "CameraUnitMetadataSet",
                ],
                "VendorMetaItemName": [
                    "VersionID",
                    "LatitudeRef",
                    "Latitude",
                    "LongitudeRef",
                    "Longitude",
                    "DateStamp",
                    "TimeStamp",
                    "Status",
                    "CaptureGammaEquation",
                ],
                "VendorMetaItemValue": [
                    "2.2.0.0",
                    "N",
                    "48;59;58.213",
                    "W",
                    "122;44;25.579",
                    "2026:07:08",
                    "22:46:21.000",
                    "A",
                    "rec709",
                ],
            }
        ),
        fact_extractors=[
            {
                "type": "name_value",
                "name_tag": "VendorMetaItemName",
                "value_tag": "VendorMetaItemValue",
                "requires": [
                    {"tag": "VendorMetaGroupName", "contains": "GpsGroup"},
                ],
                "fields": {
                    "Latitude": "exif.gps_latitude",
                    "LatitudeRef": "exif.gps_latitude_ref",
                    "Longitude": "exif.gps_longitude",
                    "LongitudeRef": "exif.gps_longitude_ref",
                },
            }
        ],
    )

    assert facts["exif.gps_latitude"] == "48;59;58.213"
    assert facts["exif.gps_latitude_ref"] == "N"
    assert facts["exif.gps_longitude"] == "122;44;25.579"
    assert facts["exif.gps_longitude_ref"] == "W"
    assert facts["exiftool"]["tags"]["vendor_meta_group_name"][0] == "GpsGroup"


def test_exiftool_routing_facts_extracts_name_value_copy_groups() -> None:
    facts = exiftool_routing_facts(
        routing_exiftool_summary(
            {
                "XMP:VendorMetaGroupName": "GpsGroup",
                "XMP:Copy1:VendorMetaGroupName": "CameraUnitMetadataSet",
                "XMP:VendorMetaItemName": "VersionID",
                "XMP:Copy1:VendorMetaItemName": "LatitudeRef",
                "XMP:Copy2:VendorMetaItemName": "Latitude",
                "XMP:Copy3:VendorMetaItemName": "LongitudeRef",
                "XMP:Copy4:VendorMetaItemName": "Longitude",
                "XMP:Copy5:VendorMetaItemName": "CaptureGammaEquation",
                "XMP:VendorMetaItemValue": "2.2.0.0",
                "XMP:Copy1:VendorMetaItemValue": "N",
                "XMP:Copy2:VendorMetaItemValue": "48;59;58.213",
                "XMP:Copy3:VendorMetaItemValue": "W",
                "XMP:Copy4:VendorMetaItemValue": "122;44;25.579",
                "XMP:Copy5:VendorMetaItemValue": "rec709",
            }
        ),
        fact_extractors=[
            {
                "type": "name_value",
                "name_tag": "VendorMetaItemName",
                "value_tag": "VendorMetaItemValue",
                "requires": [
                    {"tag": "VendorMetaGroupName", "contains": "GpsGroup"},
                ],
                "fields": {
                    "Latitude": "exif.gps_latitude",
                    "LatitudeRef": "exif.gps_latitude_ref",
                    "Longitude": "exif.gps_longitude",
                    "LongitudeRef": "exif.gps_longitude_ref",
                },
            }
        ],
    )

    assert facts["exif.gps_latitude"] == "48;59;58.213"
    assert facts["exif.gps_latitude_ref"] == "N"
    assert facts["exif.gps_longitude"] == "122;44;25.579"
    assert facts["exif.gps_longitude_ref"] == "W"
    assert facts["exiftool"]["tags"]["vendor_meta_item_name"] == [
        "VersionID",
        "LatitudeRef",
        "Latitude",
        "LongitudeRef",
        "Longitude",
        "CaptureGammaEquation",
    ]


def test_exiftool_routing_facts_keeps_repeated_embedded_gps_scalar() -> None:
    facts = exiftool_routing_facts(
        routing_exiftool_summary(
            {
                "Composite:GPSLatitude": "48 deg 59' 58.21\"",
                "Composite:Copy1:GPSLatitude": "48 deg 59' 58.21\" N",
                "Composite:GPSLatitudeRef": "North",
                "Composite:GPSLongitude": "122 deg 44' 25.58\"",
                "Composite:Copy1:GPSLongitude": "122 deg 44' 25.58\" W",
                "Composite:GPSLongitudeRef": "West",
                "Composite:GPSPosition": ("48 deg 59' 58.21\" N, 122 deg 44' 25.58\" W"),
            }
        )
    )

    assert facts["exif.gps_latitude"] == "48 deg 59' 58.21\""
    assert facts["exif.gps_latitude_ref"] == "North"
    assert facts["exif.gps_longitude"] == "122 deg 44' 25.58\""
    assert facts["exif.gps_longitude_ref"] == "West"
    assert facts["exif.gps_position"] == "48 deg 59' 58.21\" N, 122 deg 44' 25.58\" W"
    assert facts["exiftool"]["tags"]["gps_latitude"] == [
        "48 deg 59' 58.21\"",
        "48 deg 59' 58.21\" N",
    ]

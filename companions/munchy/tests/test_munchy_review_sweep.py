from __future__ import annotations

import pytest
from munchy_workflows.review_sweep import review_sweep_variants


def test_review_sweep_axes_generate_encode_profile_variants() -> None:
    base_profile = {
        "schema_version": 1,
        "target": "munchy-av1-nvenc",
        "name": "camera-base",
        "archive": {
            "codec": "av1",
            "container": "webm",
            "quality": 40,
            "audio": {"bitrate": "96k"},
        },
    }

    variants = review_sweep_variants(
        {
            "axes": {
                "archive.quality": "24..28:4",
                "archive.max_height": [720, 1080],
            }
        },
        base_profile=base_profile,
        route_id="camera-video",
    )

    assert [variant["profile_id"] for variant in variants] == [
        "q24-h720",
        "q24-h1080",
        "q28-h720",
        "q28-h1080",
    ]
    assert variants[2]["encode_profile"]["archive"]["quality"] == 28
    assert variants[2]["encode_profile"]["archive"]["max_height"] == 720
    assert variants[2]["encode_profile"]["archive"]["audio"]["bitrate"] == "96k"


def test_review_sweep_supports_audio_bitrate_and_explicit_variants() -> None:
    base_profile = {
        "schema_version": 1,
        "target": "munchy-audio",
        "name": "voice-base",
        "archive": {
            "codec": "opus",
            "container": "opus",
            "audio": {"bitrate": "32k", "sample_rate": 24000},
        },
    }

    variants = review_sweep_variants(
        {
            "audio_bitrate": ["48k", "64k"],
            "variants": [
                {
                    "profile_id": "speech-80k",
                    "encode_settings": {
                        "archive": {
                            "audio": {
                                "bitrate": "80k",
                                "application": "voip",
                            }
                        }
                    },
                }
            ],
        },
        base_profile=base_profile,
        route_id="voice",
    )

    assert [variant["profile_id"] for variant in variants] == [
        "a48k",
        "a64k",
        "speech-80k",
    ]
    assert variants[0]["encode_profile"]["archive"]["audio"]["bitrate"] == "48k"
    assert variants[1]["encode_profile"]["archive"]["audio"]["sample_rate"] == 24000
    assert variants[2]["encode_profile"]["archive"]["audio"]["application"] == "voip"


def test_review_sweep_rejects_duplicate_variant_ids() -> None:
    base_profile = {
        "schema_version": 1,
        "target": "munchy-av1-nvenc",
        "archive": {"codec": "av1", "container": "webm"},
    }

    with pytest.raises(ValueError, match="duplicate review sweep profile_id"):
        review_sweep_variants(
            {
                "variants": [
                    {"profile_id": "same", "encode_settings": {"archive": {"quality": 24}}},
                    {"profile_id": "same", "encode_settings": {"archive": {"quality": 28}}},
                ]
            },
            base_profile=base_profile,
            route_id="camera-video",
        )


def test_review_sweep_explicit_variant_profile_id_controls_profile_name() -> None:
    base_profile = {
        "schema_version": 1,
        "target": "munchy-av1-nvenc",
        "name": "base",
        "archive": {"codec": "av1", "container": "webm", "quality": 36},
    }

    variants = review_sweep_variants(
        {
            "variants": [
                {
                    "profile_id": "q40",
                    "encode_settings": {
                        "name": "stale-private-name",
                        "archive": {"quality": 40},
                    },
                }
            ]
        },
        base_profile=base_profile,
        route_id="camera-video",
    )

    assert variants[0]["encode_profile"]["name"] == "q40"
    assert variants[0]["encode_profile"]["archive"]["quality"] == 40

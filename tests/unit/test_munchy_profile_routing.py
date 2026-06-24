from __future__ import annotations

from munchy.profile_routing import match_profile_route


def test_profile_routing_uses_ordered_first_match_with_broad_matcher() -> None:
    routing = {
        "routes": [
            {
                "id": "iphone-native-video",
                "group": "video",
                "path_prefix": "nash-iphone-se2",
                "suffixes": [".mov"],
            },
            {
                "id": "iphone-library-review",
                "group": "library-review",
                "path_prefix": "nash-iphone-se2",
            },
        ]
    }

    native = match_profile_route(routing, "nash-iphone-se2/IMG_0001.MOV")
    saved = match_profile_route(routing, "nash-iphone-se2/imported/clip.mp4")
    other_source = match_profile_route(routing, "katie-pixel-8/imported/clip.mp4")

    assert native is not None
    assert native.route_id == "iphone-native-video"
    assert native.group == "video"
    assert saved is not None
    assert saved.route_id == "iphone-library-review"
    assert saved.group == "library-review"
    assert other_source is None


def test_profile_routing_broad_matcher_can_intentionally_preempt_specific_routes() -> None:
    routing = {
        "routes": [
            {
                "id": "phone-library-review",
                "group": "library-review",
                "path_prefix": "phone",
            },
            {
                "id": "phone-video",
                "group": "video",
                "path_prefix": "phone",
                "suffixes": [".mov"],
            },
        ]
    }

    match = match_profile_route(routing, "phone/IMG_0001.MOV")

    assert match is not None
    assert match.route_id == "phone-library-review"
    assert match.group == "library-review"


def test_profile_routing_uses_probe_requirements_before_falling_through() -> None:
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
                "path_prefix": "phone",
                "suffixes": [".mov"],
                "codec_names": ["h264"],
            },
            {
                "id": "iphone-video-review",
                "group": "video-review",
                "path_prefix": "phone",
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


def test_profile_routing_can_match_complicated_mov_probe_facts() -> None:
    def load_probe() -> dict[str, object]:
        return {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "format_long_name": "QuickTime / MOV",
            "duration": 1.55,
            "format_duration": 1.55,
            "format_bit_rate": 16_000_000,
            "format_tags": {
                "com.apple.quicktime.model": "iPhone SE (2nd generation)",
                "compatible_brands": "qt  ",
            },
            "has_video": True,
            "has_audio": True,
            "video_stream_count": 1,
            "audio_stream_count": 1,
            "codec_name": "hevc",
            "video_codec_name": "hevc",
            "video_profile": "main",
            "video_pix_fmt": "yuv420p",
            "video_color_range": "tv",
            "video_color_space": "bt709",
            "width": 1440,
            "height": 1080,
            "fps": 30.0,
            "audio_codec_name": "aac",
            "audio_sample_rate": 44100,
            "audio_channels": 1,
            "video_tags": {"handler_name": "Core Media Video"},
            "audio_tags": {"handler_name": "Core Media Audio"},
            "stream_tags": {"handler_name": "Core Media Video"},
        }

    routing = {
        "routes": [
            {
                "id": "iphone-live-photo-sidecar",
                "group": "live-photo-sidecar",
                "path_prefix": "phone",
                "suffixes": [".mov"],
                "format_name_contains": ["mov"],
                "video_codec_names": ["hevc"],
                "audio_codec_names": ["aac"],
                "has_audio": True,
                "video_stream_count": 1,
                "audio_stream_count": 1,
                "max_duration": 3.0,
                "min_width": 1000,
                "max_width": 2000,
                "pix_fmts": ["yuv420p"],
                "format_tags": {"com.apple.quicktime.model": "iphone se"},
            },
            {
                "id": "iphone-video-review",
                "group": "video-review",
                "path_prefix": "phone",
            },
        ]
    }

    match = match_profile_route(
        routing,
        "phone/IMG_0001.MOV",
        probe_summary_loader=load_probe,
    )

    assert match is not None
    assert match.route_id == "iphone-live-photo-sidecar"

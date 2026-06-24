from __future__ import annotations

from munchy.profile_routing import match_profile_route


def test_profile_routing_uses_ordered_first_match_with_catchall() -> None:
    routing = {
        "routes": [
            {
                "id": "iphone-native-video",
                "group": "video",
                "path_prefix": "nash-iphone-se2",
                "suffixes": [".mov"],
            },
            {
                "id": "iphone-library-catchall",
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
    assert saved.route_id == "iphone-library-catchall"
    assert saved.group == "library-review"
    assert other_source is None


def test_profile_routing_catchall_can_intentionally_preempt_specific_routes() -> None:
    routing = {
        "routes": [
            {
                "id": "phone-catchall",
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
    assert match.route_id == "phone-catchall"
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
                "id": "iphone-video-catchall",
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
    assert match.route_id == "iphone-video-catchall"

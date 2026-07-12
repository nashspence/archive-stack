from __future__ import annotations

from pathlib import Path

from munchy.job_authoring import (
    build_review_sweep_plan,
    build_runner_upload_request_from_files,
    requested_archive_containers,
)
from munchy.runner_client import RunnerInputFile


def test_build_runner_upload_request_from_files_normalizes_review_sweep() -> None:
    source = RunnerInputFile(
        source=Path("/tmp/clip.mp4"),
        rel_path="camera-video/clip.mp4",
        bytes=5,
        sha256="0" * 64,
    )

    request = build_runner_upload_request_from_files(
        [source],
        config={
            "job": {
                "workflow_mode": "review",
                "run_id": "20260712T120000Z",
                "archive_mode": "av1_nvenc",
                "tasks": ["archive_video", "qcut_video"],
                "review": {
                    "device_id": "example-camera",
                    "target": {"enabled": True, "destination": "reviews/{profile_id}"},
                    "sweep": {
                        "route_ids": ["camera-video"],
                        "variants": [
                            {
                                "profile_id": "webm-q36",
                                "encode_settings": {
                                    "archive": {
                                        "codec": "av1_nvenc",
                                        "container": "webm",
                                        "quality": 36,
                                    }
                                },
                            }
                        ],
                    },
                },
            },
            "groups": {
                "camera-video": {
                    "archive_mode": "av1_nvenc",
                    "tasks": ["archive_video", "qcut_video"],
                    "encode_profile": {
                        "schema_version": 1,
                        "target": "munchy-av1-nvenc",
                        "name": "camera-video-webm-q36",
                        "archive": {
                            "codec": "av1_nvenc",
                            "container": "webm",
                            "quality": 36,
                        },
                    },
                }
            },
        },
        collection_timestamp="20260712T120000Z",
        job_id="review-job",
        upload_id="review-upload",
        group="camera-video",
        workflow_mode="review",
        collection_archive_destination="target",
    )

    assert request.job_id == "review-job"
    assert request.upload_id == "review-upload"
    assert request.storage_hint["collection_archive_destination"] == "target"
    assert request.job_payload["tasks"] == ["qcut_video"]
    assert request.job_payload["review"]["sweep"]["variants"][0]["profile_id"] == "webm-q36"
    assert request.job_payload["groups"]["camera-video"]["tasks"] == ["qcut_video"]
    assert requested_archive_containers(request) == ["webm"]


def test_build_review_sweep_plan_expands_configured_routes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    source_dir = tmp_path / "camera"
    source_dir.mkdir()
    (source_dir / "clip.mp4").write_bytes(b"video")
    (source_dir / "photo.jpg").write_bytes(b"photo")
    config = {
        "job": {
            "workflow_mode": "review",
            "collection_timestamp": "20260712T120000Z",
            "review": {
                "device_id": "camera",
                "target": {
                    "enabled": True,
                    "method": "rclone",
                    "destination": "clover:reviews/{device_id}/{route_id}/{profile_id}/{run_id}",
                },
                "sweep": {"quality": "24..28:4"},
            },
            "routing": {
                "routes": [
                    {
                        "id": "camera-video",
                        "group": "video",
                        "when": {"path": {"suffix": ".mp4"}},
                    },
                    {
                        "id": "camera-photo",
                        "group": "preserve",
                        "when": {"path": {"suffix": ".jpg"}},
                    },
                ]
            },
        },
        "profiles": {
            "video": {
                "schema_version": 1,
                "target": "munchy-av1-nvenc",
                "name": "video",
                "archive": {
                    "codec": "av1_nvenc",
                    "container": "webm",
                    "quality": 40,
                },
            }
        },
        "groups": {
            "video": {
                "profile": "video",
                "archive_mode": "av1_nvenc",
                "tasks": ["archive_video", "qcut_video"],
            },
            "preserve": {"archive_mode": "preserve", "tasks": []},
        },
    }

    plan = build_review_sweep_plan(source=source_dir, config=config)

    assert plan["ok"] is True
    assert plan["routes_total"] == 1
    assert plan["files_total"] == 1
    assert plan["variants_total"] == 2
    route = plan["routes"][0]
    assert route["route_id"] == "camera-video"
    assert [variant["profile_id"] for variant in route["variants"]] == ["q24", "q28"]
    assert route["variants"][0]["destination"] == (
        "clover:reviews/camera/camera-video/q24/20260712T120000Z"
    )

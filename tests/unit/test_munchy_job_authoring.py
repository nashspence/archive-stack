from __future__ import annotations

from pathlib import Path

from munchy.job_authoring import (
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

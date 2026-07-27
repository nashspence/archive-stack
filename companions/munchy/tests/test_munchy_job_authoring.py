from __future__ import annotations

from pathlib import Path

import pytest
from config_validation import ConfigError
from munchy_config import instantiate_device_profile, load_device_profile
from munchy_workflows.job_authoring import (
    build_review_sweep_plan,
    load_munchy_job_config,
    load_munchy_job_definition,
    munchy_job_defaults_from_config,
)


def test_munchy_job_defaults_from_config_lowers_public_config() -> None:
    defaults = munchy_job_defaults_from_config(
        {
            "schema_version": 1,
            "kind": "munchy.job",
            "job": {
                "workflow_mode": "collection_archive",
                "routing": {
                    "routes": [
                        {
                            "id": "camera-video",
                            "group": "video",
                            "when": {"path": {"suffix": ".mp4"}},
                        }
                    ]
                },
            },
            "profiles": {
                "camera": {
                    "schema_version": 1,
                    "target": "munchy-av1-nvenc",
                    "name": "camera",
                    "archive": {"codec": "av1_nvenc", "container": "webm", "quality": 36},
                }
            },
            "groups": {
                "video": {
                    "profile": "camera",
                    "output_mode": "video",
                    "metadata_projection": {"tags": ["device/example-camera"]},
                }
            },
        }
    )

    assert defaults["workflow_mode"] == "collection_archive"
    assert defaults["routing"]["routes"][0]["id"] == "camera-video"
    assert defaults["groups"]["video"]["tasks"] == ["archive_video", "qcut_video", "audio_review"]
    assert defaults["groups"]["video"]["encode_profile"]["archive"]["container"] == "webm"
    assert defaults["groups"]["video"]["metadata_projection"] == {"tags": ["device/example-camera"]}


def test_munchy_job_config_loads_device_profile_by_relative_path(tmp_path: Path) -> None:
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "example-camera.yaml").write_text(
        """
schema_version: 1
kind: munchy.device_profile
id: example-camera
parameters:
  device_id:
    required: true
section:
  destination_prefix: '{device_id}-media'
  group: video
  output_mode: video
  encode_profile:
    schema_version: 1
    target: munchy-av1-nvenc
    name: '{device_id}-webm'
    archive:
      codec: av1_nvenc
      container: webm
      quality: 36
  routing:
    sidecars:
      xmp:
        path: '{parent}/{stem}.xmp'
        primary:
          path:
            suffix: .mp4
        sidecar:
          path:
            suffix: .xmp
    gates:
      '{device_id}-native':
        path:
          basename_regex: ^CLIP[0-9]{6}\\.MP4$
    routes:
      - id: video
        group: video
        into: video
        when:
          gate: '{device_id}-native'
""".strip(),
        encoding="utf-8",
    )
    path = tmp_path / "munchy.yaml"
    path.write_text(
        """
schema_version: 1
kind: munchy.job
device_profile:
  path: profiles/example-camera.yaml
  parameters:
    device_id: patio-camera
  overrides:
    groups:
      video:
        max_parallel_encodes: 2
job:
  workflow_mode: collection_archive
  handoff:
    destination: riverhog
""".strip(),
        encoding="utf-8",
    )

    definition = load_munchy_job_definition(path)
    assert isinstance(definition["job"]["routing"]["sidecars"], dict)
    assert isinstance(definition["job"]["routing"]["gates"], dict)
    assert "device_profile" not in definition

    config = load_munchy_job_config(path)
    defaults = munchy_job_defaults_from_config(config)

    assert defaults["destination_prefix"] == "patio-camera-media"
    assert defaults["routing"]["gates"]["patio-camera-native"] == {
        "path": {"basename_regex": r"^CLIP[0-9]{6}\.MP4$"}
    }
    assert defaults["routing"]["sidecars"][0]["path"] == "{parent}/{stem}.xmp"
    assert defaults["routing"]["routes"][0]["id"] == "video"
    assert defaults["groups"]["video"]["max_parallel_encodes"] == 2
    assert defaults["groups"]["video"]["encode_profile"]["archive"]["quality"] == 36


def test_public_device_profiles_validate_and_instantiate() -> None:
    profile_dir = Path(__file__).parents[1] / "config" / "device-profiles"
    for path in sorted(profile_dir.glob("*.yaml")):
        profile = load_device_profile(path)
        parameters = {
            str(name): {
                "device_id": "example-camera",
                "camera_name": "Example Camera",
                "timezone": "UTC",
            }.get(str(name), f"example-{name}")
            for name in profile.get("parameters", {})
        }
        section = instantiate_device_profile(profile, parameters=parameters, label=str(path))
        assert isinstance(section, dict)
        assert section


def test_munchy_device_profile_requires_declared_parameters(tmp_path: Path) -> None:
    profile = tmp_path / "example-camera.yaml"
    profile.write_text(
        """
schema_version: 1
kind: munchy.device_profile
id: example-camera
parameters:
  device_id:
    required: true
section:
  routing:
    routes:
      - id: video
        group: video
        when:
          path:
            prefix: '{device_id}'
""".strip(),
        encoding="utf-8",
    )
    path = tmp_path / "munchy.yaml"
    path.write_text(
        f"""
schema_version: 1
kind: munchy.job
device_profile:
  path: {profile.name}
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="requires device profile parameter 'device_id'"):
        load_munchy_job_config(path)


def test_build_review_sweep_plan_expands_configured_routes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    source_dir = tmp_path / "camera"
    source_dir.mkdir()
    (source_dir / "clip.mp4").write_bytes(b"video")
    (source_dir / "photo.jpg").write_bytes(b"photo")
    config = {
        "job": {
            "workflow_mode": "review",
            "run_id": "20260712T120000Z",
            "handoff": {
                "destination": "rclone",
                "options": {
                    "location": (
                        "review-remote:reviews/{template_id}/{route_id}/{profile_id}/{run_id}"
                    ),
                },
            },
            "review": {
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
                "output_mode": "video",
                "tasks": ["archive_video", "qcut_video"],
            },
            "preserve": {"output_mode": "preserve", "tasks": []},
        },
    }

    plan = build_review_sweep_plan(
        source=source_dir,
        template_id="camera-review-sweep",
        config=config,
    )

    assert plan["ok"] is True
    assert plan["routes_total"] == 1
    assert plan["files_total"] == 1
    assert plan["variants_total"] == 2
    route = plan["routes"][0]
    assert route["route_id"] == "camera-video"
    assert [variant["profile_id"] for variant in route["variants"]] == ["q24", "q28"]
    assert route["variants"][0]["location"] == (
        "review-remote:reviews/camera-review-sweep/camera-video/q24/20260712T120000Z"
    )

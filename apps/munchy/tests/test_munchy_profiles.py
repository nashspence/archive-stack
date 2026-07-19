from __future__ import annotations

import pytest
from munchy.profiles import (
    EncodeProfile,
    ProfileError,
    artifact_drop_reason_map,
    load_encode_profile,
    load_encode_profiles,
    normalize_artifact_drop_selector,
)
from pydantic import ValidationError


def test_normalizes_supported_artifact_drop_selectors() -> None:
    assert normalize_artifact_drop_selector(" Stream:7 ") == "stream:7"
    assert normalize_artifact_drop_selector("ATOM:Meta:2") == "atom:meta:2"
    assert normalize_artifact_drop_selector("top-level-atom:UUID") == "top-level-atom:uuid"
    assert normalize_artifact_drop_selector("atom-offset:0x1A") == "atom-offset:0x1a"


def test_rejects_invalid_artifact_drop_selector() -> None:
    with pytest.raises(ProfileError, match="artifact drop selector"):
        normalize_artifact_drop_selector("metadata:everything")


def test_validates_av1_nvenc_profile_and_drop_reasons() -> None:
    profile = EncodeProfile.model_validate(
        {
            "schema_version": 1,
            "target": "munchy-av1-nvenc",
            "name": "camera-archive",
            "archive": {
                "codec": "av1_nvenc",
                "container": "webm",
                "quality": 52,
                "preset": "p6",
                "audio": {"bitrate": "28k", "frame_duration": 40},
            },
            "source": {
                "allow_conversion_only_container": True,
                "artifact_drops": [
                    {"selector": "stream:4", "reason": "gyro data intentionally discarded"}
                ],
            },
        }
    )

    assert profile.archive.container == "webm"
    assert profile.archive.quality == 52
    assert profile.archive.audio.frame_duration == 40
    assert profile.source is not None
    assert profile.source.allow_conversion_only_container is True
    assert artifact_drop_reason_map(profile) == {"stream:4": "gyro data intentionally discarded"}


def test_validates_audio_opus_profile() -> None:
    profile = EncodeProfile.model_validate(
        {
            "schema_version": 1,
            "target": "munchy-audio",
            "name": "voice-recorder",
            "archive": {
                "codec": "opus",
                "audio": {
                    "bitrate": "64k",
                    "sample_rate": 24000,
                    "channels": 1,
                    "application": "audio",
                },
            },
        }
    )

    assert profile.target == "munchy-audio"
    assert profile.archive.codec == "opus"
    assert profile.archive.container == "opus"
    assert profile.archive.audio.bitrate == "64k"
    assert profile.archive.audio.sample_rate == 24000


def test_infers_audio_target_from_opus_codec() -> None:
    profile = EncodeProfile.model_validate(
        {
            "name": "voice-recorder",
            "archive": {
                "codec": "opus",
                "audio": {"bitrate": "28k", "sample_rate": 24000, "channels": 1},
            },
        }
    )

    assert profile.target == "munchy-audio"
    assert profile.archive.container == "opus"


def test_rejects_audio_profile_with_video_container() -> None:
    with pytest.raises(ValidationError, match="opus audio archive container must be opus"):
        EncodeProfile.model_validate(
            {
                "target": "munchy-audio",
                "archive": {"codec": "opus", "container": "webm"},
            }
        )


def test_rejects_audio_target_without_audio_codec() -> None:
    with pytest.raises(ValidationError, match="munchy-audio profiles require"):
        EncodeProfile.model_validate({"target": "munchy-audio"})


def test_rejects_duplicate_artifact_drops() -> None:
    with pytest.raises(ValidationError, match="duplicate source artifact drop selector"):
        EncodeProfile.model_validate(
            {
                "source": {
                    "artifact_drops": [
                        {"selector": "stream:4", "reason": "first"},
                        {"selector": "STREAM:4", "reason": "second"},
                    ]
                }
            }
        )


def test_loads_standalone_runner_encode_profile_document(tmp_path) -> None:  # type: ignore[no-untyped-def]
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(
        """
schema_version: 1
target: munchy-av1-nvenc
name: camera-archive
archive:
  codec: av1_nvenc
  container: mkv
  quality: 49
""".strip(),
        encoding="utf-8",
    )

    profile = load_encode_profile(profile_path)

    assert profile.target == "munchy-av1-nvenc"
    assert profile.name == "camera-archive"
    assert profile.archive.container == "mkv"
    assert profile.archive.quality == 49


def test_loads_profiles_from_runner_job_config(tmp_path) -> None:  # type: ignore[no-untyped-def]
    config_path = tmp_path / "job.yaml"
    config_path.write_text(
        """
profiles:
  camera:
    schema_version: 1
    target: munchy-av1-nvenc
    name: camera
    archive:
      codec: av1_nvenc
      container: webm
      quality: 38
  preserve:
    schema_version: 1
    target: munchy-av1-nvenc
    name: preserve
""".strip(),
        encoding="utf-8",
    )

    profiles = load_encode_profiles(config_path)

    assert set(profiles) == {"camera", "preserve"}
    assert profiles["camera"].archive.container == "webm"
    assert profiles["camera"].archive.quality == 38

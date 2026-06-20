from __future__ import annotations

import pytest
from pydantic import ValidationError

from munchy.profiles import (
    EncodeProfile,
    ProfileError,
    artifact_drop_reason_map,
    load_encode_profile,
    normalize_artifact_drop_selector,
)


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
            "target": "munchy-av1-nvenc",
            "archive": {
                "container": "webm",
                "video": {"quality": 52, "preset": "p6"},
                "audio": {"bitrate": "28k"},
            },
            "source": {
                "artifact_drops": [
                    {"selector": "stream:4", "reason": "gyro data intentionally discarded"}
                ]
            },
        }
    )

    assert profile.archive.container == "webm"
    assert profile.archive.video.quality == 52
    assert artifact_drop_reason_map(profile) == {"stream:4": "gyro data intentionally discarded"}


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


def test_loads_nested_encode_profile_document(tmp_path) -> None:  # type: ignore[no-untyped-def]
    profile_path = tmp_path / "profile.toml"
    profile_path.write_text(
        """
[encode_profile]
target = "munchy-av1-nvenc"

[encode_profile.archive]
container = "mkv"

[encode_profile.archive.video]
quality = 49
""".strip(),
        encoding="utf-8",
    )

    profile = load_encode_profile(profile_path)

    assert profile.target == "munchy-av1-nvenc"
    assert profile.archive.container == "mkv"
    assert profile.archive.video.quality == 49

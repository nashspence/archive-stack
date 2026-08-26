from __future__ import annotations

import math

import pytest
from stove0_media_archive_target_contracts import (
    AUDIO_ARCHIVE_INTENT_SEMANTICS,
    AUDIO_ARCHIVE_OPERATION,
    AV1_OPUS_ARCHIVE_INTENT_SEMANTICS,
    AV1_OPUS_ARCHIVE_OPERATION,
    SOURCE_ARTIFACT_ROLE,
    Av1OpusArchiveIntent,
    validate_av1_opus_archive_intent,
)


def test_media_archive_operations_retain_exact_v1_retirement_semantics() -> None:
    source_artifacts = next(
        output
        for output in AV1_OPUS_ARCHIVE_OPERATION.outputs
        if output.role == SOURCE_ARTIFACT_ROLE
    )

    assert AUDIO_ARCHIVE_OPERATION.id == "stove0.media.audio-archive/v1"
    assert (
        AUDIO_ARCHIVE_OPERATION.contract_sha256
        == "1181702c11274dde8f3401c202bcad6b6a4bf8cc8d0eb3a1289e7493f5c31b10"
    )
    assert AUDIO_ARCHIVE_OPERATION.source_retirement_permitted is False
    assert AV1_OPUS_ARCHIVE_OPERATION.id == "stove0.media.av1-opus-archive/v1"
    assert (
        AV1_OPUS_ARCHIVE_OPERATION.contract_sha256
        == "020c6c9820057200ded67b419cd3e0ea11587e7953a9cd4939dfd3fc556c3080"
    )
    assert AV1_OPUS_ARCHIVE_OPERATION.source_retirement_permitted is True
    assert source_artifacts.minimum == 1
    assert "preserve_source_artifacts" not in Av1OpusArchiveIntent.model_json_schema()["properties"]
    assert AUDIO_ARCHIVE_OPERATION.intent_semantics == AUDIO_ARCHIVE_INTENT_SEMANTICS
    assert AV1_OPUS_ARCHIVE_OPERATION.intent_semantics == AV1_OPUS_ARCHIVE_INTENT_SEMANTICS


def test_media_archive_semantic_profile_executes_projection_rules() -> None:
    with pytest.raises(ValueError, match="GPS coordinates"):
        validate_av1_opus_archive_intent(
            {
                "metadata_projection": {
                    "gps": {"latitude": math.nan, "longitude": 0.0},
                }
            }
        )

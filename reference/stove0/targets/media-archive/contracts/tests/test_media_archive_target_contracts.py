from __future__ import annotations

import math

import pytest
from stove0_media_archive_target_contracts import (
    AUDIO_ARCHIVE_INTENT_CONFORMANCE_VECTORS,
    AUDIO_ARCHIVE_INTENT_SEMANTICS,
    AUDIO_ARCHIVE_OPERATION,
    AV1_OPUS_ARCHIVE_INTENT_CONFORMANCE_VECTORS,
    AV1_OPUS_ARCHIVE_INTENT_SEMANTICS,
    AV1_OPUS_ARCHIVE_OPERATION,
    SOURCE_ARTIFACT_ROLE,
    Av1OpusArchiveIntent,
    validate_audio_archive_intent,
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
        == "e756d97c1b30f97d64fe82b1279c6605f7eac15af8677aecd429dc328b539d25"
    )
    assert AUDIO_ARCHIVE_OPERATION.source_retirement_permitted is False
    assert AV1_OPUS_ARCHIVE_OPERATION.id == "stove0.media.av1-opus-archive/v1"
    assert (
        AV1_OPUS_ARCHIVE_OPERATION.contract_sha256
        == "1823db9b9ec9c99e44ec740e6d56a8d6e3096c9d42b19897e0a7750e18d9835b"
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


def test_media_archive_semantic_vectors_are_bound_and_executable() -> None:
    assert AUDIO_ARCHIVE_INTENT_CONFORMANCE_VECTORS.profile_id == (
        AUDIO_ARCHIVE_INTENT_SEMANTICS.id
    )
    assert AUDIO_ARCHIVE_INTENT_SEMANTICS.conformance_vectors_sha256 == (
        AUDIO_ARCHIVE_INTENT_CONFORMANCE_VECTORS.sha256
    )
    for vector in AUDIO_ARCHIVE_INTENT_CONFORMANCE_VECTORS.vectors:
        if vector.accepted:
            validate_audio_archive_intent(vector.intent)
        else:
            with pytest.raises(ValueError):
                validate_audio_archive_intent(vector.intent)

    assert AV1_OPUS_ARCHIVE_INTENT_CONFORMANCE_VECTORS.profile_id == (
        AV1_OPUS_ARCHIVE_INTENT_SEMANTICS.id
    )
    assert AV1_OPUS_ARCHIVE_INTENT_SEMANTICS.conformance_vectors_sha256 == (
        AV1_OPUS_ARCHIVE_INTENT_CONFORMANCE_VECTORS.sha256
    )
    for vector in AV1_OPUS_ARCHIVE_INTENT_CONFORMANCE_VECTORS.vectors:
        if vector.accepted:
            validate_av1_opus_archive_intent(vector.intent)
        else:
            with pytest.raises(ValueError):
                validate_av1_opus_archive_intent(vector.intent)

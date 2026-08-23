from __future__ import annotations

from stove0_media_archive_contracts import (
    AUDIO_ARCHIVE_OPERATION,
    AV1_OPUS_ARCHIVE_OPERATION,
    SOURCE_ARTIFACT_ROLE,
    Av1OpusArchiveIntent,
)


def test_audio_only_archive_cannot_retire_its_richer_source_collection() -> None:
    assert AUDIO_ARCHIVE_OPERATION.source_retirement_permitted is False


def test_archive_video_retirement_always_requires_reconstructive_source_artifacts() -> None:
    source_artifacts = next(
        output
        for output in AV1_OPUS_ARCHIVE_OPERATION.outputs
        if output.role == SOURCE_ARTIFACT_ROLE
    )

    assert AV1_OPUS_ARCHIVE_OPERATION.source_retirement_permitted
    assert source_artifacts.minimum == 1
    assert "preserve_source_artifacts" not in Av1OpusArchiveIntent.model_json_schema()["properties"]

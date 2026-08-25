from __future__ import annotations

from stove0_media_archive_target_contracts import (
    AUDIO_ARCHIVE_OPERATION,
    AV1_OPUS_ARCHIVE_OPERATION,
    SOURCE_ARTIFACT_ROLE,
    Av1OpusArchiveIntent,
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
        == "9809e2591b4c73f8e792def9dc5b23339045231bd5177034dc7324fd2b89f818"
    )
    assert AUDIO_ARCHIVE_OPERATION.source_retirement_permitted is False
    assert AV1_OPUS_ARCHIVE_OPERATION.id == "stove0.media.av1-opus-archive/v1"
    assert (
        AV1_OPUS_ARCHIVE_OPERATION.contract_sha256
        == "d4596612f4f55652ec5fc75dbd30333ac092cff6b204e6639cefdea6cd8ff769"
    )
    assert AV1_OPUS_ARCHIVE_OPERATION.source_retirement_permitted is True
    assert source_artifacts.minimum == 1
    assert "preserve_source_artifacts" not in Av1OpusArchiveIntent.model_json_schema()["properties"]

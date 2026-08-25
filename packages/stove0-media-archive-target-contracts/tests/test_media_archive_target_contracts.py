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
        == "7f778baa583e426e985ea4806bd447cf8b029bcb6b78e528a039a59426a8e0e1"
    )
    assert AUDIO_ARCHIVE_OPERATION.source_retirement_permitted is False
    assert AV1_OPUS_ARCHIVE_OPERATION.id == "stove0.media.av1-opus-archive/v1"
    assert (
        AV1_OPUS_ARCHIVE_OPERATION.contract_sha256
        == "a455d6a2525da3a3355758b9307fa31c2fdda664d515f1635b6bdf9dc9c3ec31"
    )
    assert AV1_OPUS_ARCHIVE_OPERATION.source_retirement_permitted is True
    assert source_artifacts.minimum == 1
    assert "preserve_source_artifacts" not in Av1OpusArchiveIntent.model_json_schema()["properties"]

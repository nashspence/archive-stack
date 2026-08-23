from __future__ import annotations

from stove0_media_archive_contracts import (
    AUDIO_ARCHIVE_OPERATION,
    AV1_OPUS_ARCHIVE_OPERATION,
    MEDIA_METADATA_OBSERVER_CONTRACT,
    SOURCE_ARTIFACT_ROLE,
    Av1OpusArchiveIntent,
    MediaArtifactFacts,
    MediaFactEvidence,
    MediaMetadataFact,
    MediaMetadataFacts,
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


def test_media_observation_contract_binds_each_fact_to_its_exact_artifact() -> None:
    facts = MediaMetadataFacts(
        artifacts=(
            MediaArtifactFacts(
                artifact_id="primary",
                state="observed",
                facts=(
                    MediaMetadataFact(
                        name="capture-time",
                        value="2025:02:03 04:05:06-08:00",
                        evidence=MediaFactEvidence(
                            artifact_id="primary",
                            field="XMP-xmp:CreateDate",
                        ),
                    ),
                ),
            ),
        )
    )

    schema = MEDIA_METADATA_OBSERVER_CONTRACT.facts_schema.document
    assert facts.model_dump(mode="json")["artifacts"][0]["artifact_id"] == "primary"
    assert schema["properties"]["artifacts"].get("maxItems") is None

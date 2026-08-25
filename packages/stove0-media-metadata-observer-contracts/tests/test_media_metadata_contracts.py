from __future__ import annotations

from stove0_media_metadata_observer_contracts import (
    MEDIA_METADATA_OBSERVER_CONTRACT,
    MediaArtifactFacts,
    MediaFactEvidence,
    MediaMetadataFact,
    MediaMetadataFacts,
)


def test_media_metadata_contract_binds_facts_to_exact_artifacts_without_a_ceiling() -> None:
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

    assert MEDIA_METADATA_OBSERVER_CONTRACT.id == "stove0.media.metadata/v1"
    assert (
        MEDIA_METADATA_OBSERVER_CONTRACT.contract_sha256
        == "527ff1ef0a62e8705d29dca2d35574659d983b67e5fb5a353992ee81745f5c1c"
    )
    assert facts.artifacts[0].artifact_id == "primary"
    assert (
        MEDIA_METADATA_OBSERVER_CONTRACT.facts_schema.document["properties"]["artifacts"].get(
            "maxItems"
        )
        is None
    )

from __future__ import annotations

from stove0_media_sampling_observer_contracts import (
    MEDIA_SAMPLING_OBSERVER_CONTRACT,
    MediaSamplingArtifactFacts,
    MediaSamplingFacts,
    SampleableRange,
)


def test_media_sampling_contract_has_no_artifact_or_duration_ceiling() -> None:
    facts = MediaSamplingFacts(
        artifacts=tuple(
            MediaSamplingArtifactFacts(
                artifact_id=f"camera-{index:03d}",
                duration_ms=10_000_000,
                sampleable_ranges=(SampleableRange(start_ms=0, duration_ms=10_000_000),),
            )
            for index in range(257)
        )
    )

    schema = MEDIA_SAMPLING_OBSERVER_CONTRACT.facts_schema.document
    assert MEDIA_SAMPLING_OBSERVER_CONTRACT.id == "stove0.review.media-sampling/v1"
    assert (
        MEDIA_SAMPLING_OBSERVER_CONTRACT.contract_sha256
        == "3323eb5382837ea4e10e12f18979cfe34f34a6ab3e57c04d7b1fa3db5bb00f98"
    )
    assert len(facts.artifacts) == 257
    assert schema["properties"]["artifacts"].get("maxItems") is None

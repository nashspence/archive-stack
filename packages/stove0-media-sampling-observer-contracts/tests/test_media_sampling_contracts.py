from __future__ import annotations

import pytest
from stove0_media_sampling_observer_contracts import (
    MEDIA_SAMPLING_FACTS_CONFORMANCE_VECTORS,
    MEDIA_SAMPLING_FACTS_SEMANTICS,
    MEDIA_SAMPLING_OBSERVER_CONTRACT,
    MediaSamplingArtifactFacts,
    MediaSamplingFacts,
    SampleableRange,
    validate_media_sampling_facts,
)
from stove0_observer_protocol import ArtifactSubject, CollectionRootRef


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
        == "3dffcfc9c44a555400cff878e6cda59e7d9722557a0905391658777eae637e0b"
    )
    assert len(facts.artifacts) == 257
    assert schema["properties"]["artifacts"].get("maxItems") is None


def test_media_sampling_semantic_vectors_are_bound_and_executable() -> None:
    assert MEDIA_SAMPLING_FACTS_CONFORMANCE_VECTORS.profile_id == (
        MEDIA_SAMPLING_FACTS_SEMANTICS.id
    )
    assert MEDIA_SAMPLING_FACTS_SEMANTICS.conformance_vectors_sha256 == (
        MEDIA_SAMPLING_FACTS_CONFORMANCE_VECTORS.sha256
    )
    for vector in MEDIA_SAMPLING_FACTS_CONFORMANCE_VECTORS.vectors:
        if vector.accepted:
            validate_media_sampling_facts(vector.facts, vector.subjects)
        else:
            with pytest.raises(ValueError):
                validate_media_sampling_facts(vector.facts, vector.subjects)


def test_media_sampling_semantics_bind_ranges_and_exact_request_subjects() -> None:
    subject = ArtifactSubject(
        id="camera",
        role="fixture.media/v1",
        collection=CollectionRootRef(
            collection_id=1,
            archive_root_sha256="a" * 64,
            content_identity="b" * 64,
        ),
        path="camera.mp4",
        bytes=10,
        sha256="c" * 64,
    )
    facts = {
        "artifacts": [
            {
                "artifact_id": "camera",
                "duration_ms": 1000,
                "sampleable_ranges": [{"start_ms": 0, "duration_ms": 1000}],
            }
        ]
    }

    assert validate_media_sampling_facts(facts, (subject,)).artifacts[0].artifact_id == "camera"
    with pytest.raises(ValueError, match="exact request subjects"):
        validate_media_sampling_facts(facts, (subject.model_copy(update={"id": "other"}),))
    with pytest.raises(ValueError, match="exceeds artifact duration"):
        validate_media_sampling_facts(
            {
                "artifacts": [
                    {
                        "artifact_id": "camera",
                        "duration_ms": 1000,
                        "sampleable_ranges": [{"start_ms": 500, "duration_ms": 501}],
                    }
                ]
            },
            (subject,),
        )

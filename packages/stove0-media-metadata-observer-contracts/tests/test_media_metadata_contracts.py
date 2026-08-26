from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from stove0_media_metadata_observer_contracts import (
    MEDIA_METADATA_FACTS_CONFORMANCE_VECTORS,
    MEDIA_METADATA_FACTS_SEMANTICS,
    MEDIA_METADATA_OBSERVER_CONTRACT,
    MediaArtifactFacts,
    MediaFactEvidence,
    MediaMetadataFact,
    MediaMetadataFacts,
    validate_media_metadata_facts,
)
from stove0_observer_protocol import ArtifactSubject, CollectionRootRef


def test_media_metadata_contract_carries_exact_evidence_without_a_ceiling() -> None:
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
        == "e7c4c52d0913f738ea813949066cd59fba6e1c680f5d54d9795404de03ee0435"
    )
    assert facts.artifacts[0].artifact_id == "primary"
    assert (
        MEDIA_METADATA_OBSERVER_CONTRACT.facts_schema.document["properties"]["artifacts"].get(
            "maxItems"
        )
        is None
    )


def test_media_metadata_semantic_vectors_are_bound_and_executable() -> None:
    assert MEDIA_METADATA_FACTS_CONFORMANCE_VECTORS.profile_id == (
        MEDIA_METADATA_FACTS_SEMANTICS.id
    )
    assert MEDIA_METADATA_FACTS_SEMANTICS.conformance_vectors_sha256 == (
        MEDIA_METADATA_FACTS_CONFORMANCE_VECTORS.sha256
    )
    for vector in MEDIA_METADATA_FACTS_CONFORMANCE_VECTORS.vectors:
        if vector.accepted:
            validate_media_metadata_facts(vector.facts, vector.subjects)
        else:
            with pytest.raises((ValidationError, ValueError)):
                validate_media_metadata_facts(vector.facts, vector.subjects)


def test_media_fact_model_and_published_schema_share_state_acceptance() -> None:
    document = {
        "artifacts": [
            {
                "artifact_id": "primary",
                "state": "unsupported",
                "facts": [
                    {
                        "name": "creator",
                        "value": "Example",
                        "evidence": {"artifact_id": "primary", "field": "Artist"},
                    }
                ],
            }
        ]
    }

    with pytest.raises(ValidationError, match="unsupported"):
        MediaMetadataFacts.model_validate(document)
    with pytest.raises(Exception, match="expected to be empty"):
        Draft202012Validator(MEDIA_METADATA_OBSERVER_CONTRACT.facts_schema.document).validate(
            document
        )


def test_media_metadata_semantics_bind_evidence_and_exact_request_subjects() -> None:
    subject = ArtifactSubject(
        id="primary",
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
    document = {
        "artifacts": [
            {
                "artifact_id": "primary",
                "state": "observed",
                "facts": [
                    {
                        "name": "creator",
                        "value": "Example",
                        "evidence": {"artifact_id": "primary", "field": "Artist"},
                    }
                ],
            }
        ]
    }

    assert validate_media_metadata_facts(document, (subject,)).artifacts[0].artifact_id == "primary"
    wrong_evidence = {
        "artifacts": [
            {
                "artifact_id": "primary",
                "state": "observed",
                "facts": [
                    {
                        "name": "creator",
                        "value": "Example",
                        "evidence": {"artifact_id": "other", "field": "Artist"},
                    }
                ],
            }
        ]
    }
    with pytest.raises(ValidationError, match="containing artifact"):
        validate_media_metadata_facts(wrong_evidence, (subject,))
    with pytest.raises(ValueError, match="exact request subjects"):
        validate_media_metadata_facts(
            document,
            (subject.model_copy(update={"id": "other"}),),
        )

from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from stove0_media_metadata_observer_contracts import (
    MEDIA_METADATA_OBSERVER_CONTRACT,
    MediaArtifactFacts,
    MediaFactEvidence,
    MediaMetadataFact,
    MediaMetadataFacts,
)


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
        == "c58f17cb8503ad730684d7fb00c374d5bbe1a346e44befb02bf5fa496c3e958e"
    )
    assert facts.artifacts[0].artifact_id == "primary"
    assert (
        MEDIA_METADATA_OBSERVER_CONTRACT.facts_schema.document["properties"]["artifacts"].get(
            "maxItems"
        )
        is None
    )


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

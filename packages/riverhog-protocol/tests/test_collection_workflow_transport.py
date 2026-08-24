from __future__ import annotations

import pytest
from pydantic import ValidationError
from riverhog_protocol.collection_workflow_transport import (
    CollectionArtifactIdentityDocument,
    CollectionRootIdentityDocument,
    ProcessingClaimCreateDocument,
    ProcessingClaimDocument,
    TransformCapabilityCreateDocument,
)
from riverhog_protocol.collection_workflows import (
    CollectionArtifactIdentity,
    CollectionRootIdentity,
    canonical_json_sha256,
)


def test_transport_acceptance_matches_canonical_root_and_artifact_contracts() -> None:
    root = {
        "collection_id": 17,
        "archive_root_sha256": "1" * 64,
        "content_identity": "2" * 64,
    }
    artifact = {
        "collection": root,
        "path": "camera/clip.mp4",
        "bytes": 42,
        "sha256": "3" * 64,
    }

    assert CollectionRootIdentityDocument.model_validate(root).model_dump(mode="json") == (
        CollectionRootIdentity.from_mapping(root).as_dict()
    )
    assert (
        CollectionArtifactIdentityDocument.model_validate(artifact).model_dump(mode="json")
        == CollectionArtifactIdentity.from_mapping(artifact).as_dict()
    )

    invalid = {**artifact, "path": "camera/../clip.mp4"}
    with pytest.raises(ValueError):
        CollectionArtifactIdentity.from_mapping(invalid)
    with pytest.raises(ValidationError):
        CollectionArtifactIdentityDocument.model_validate(invalid)


def test_opaque_work_document_is_digest_bound_without_application_ontology() -> None:
    document = {
        "format": "some-application-work/v1",
        "application_private": {"meaning": [1, 2, 3]},
    }
    request = ProcessingClaimCreateDocument(
        work_id="4" * 64,
        work_document=document,
        work_document_sha256=canonical_json_sha256(document),
        inputs=[
            {
                "collection_id": 17,
                "archive_root_sha256": "1" * 64,
                "content_identity": "2" * 64,
            }
        ],
    )

    assert request.work_document == document
    schema = ProcessingClaimCreateDocument.model_json_schema()
    work_schema = schema["properties"]["work_document"]
    assert work_schema["type"] == "object"
    assert "properties" not in work_schema
    assert "stove0" not in str(schema).casefold()

    with pytest.raises(ValidationError, match="identity does not match"):
        ProcessingClaimCreateDocument(
            work_id="4" * 64,
            work_document=document,
            work_document_sha256="5" * 64,
            inputs=request.inputs,
        )


def test_transform_capability_actions_are_the_exact_read_contract() -> None:
    artifact = {
        "collection": {
            "collection_id": 17,
            "archive_root_sha256": "1" * 64,
            "content_identity": "2" * 64,
        },
        "path": "camera/clip.mp4",
        "bytes": 42,
        "sha256": "3" * 64,
    }

    for actions in (["read-inputs"], ["read-inputs", "write-output"]):
        capability = TransformCapabilityCreateDocument(
            fence=1,
            audience="transform:test",
            actions=actions,
            artifacts=[artifact],
        )
        assert capability.actions == actions

    for actions in (["write-output"], ["read-inputs", "manage-output-tags"]):
        with pytest.raises(ValidationError, match="read-inputs"):
            TransformCapabilityCreateDocument(
                fence=1,
                audience="transform:test",
                actions=actions,
                artifacts=[artifact],
            )


def test_processing_claim_projection_rejects_impossible_state_evidence() -> None:
    work_document = {"format": "fixture-work/v1"}
    active = {
        "format": "riverhog-processing-claim/v1",
        "id": "4" * 64,
        "work_id": "5" * 64,
        "consumer": {"app": "fixture"},
        "purpose": "fixture",
        "state": "active",
        "fence": 1,
        "expires_at": "2026-08-24T00:15:00.000000Z",
        "created_at": "2026-08-24T00:00:00.000000Z",
        "updated_at": "2026-08-24T00:00:00.000000Z",
        "work_document": work_document,
        "work_document_sha256": canonical_json_sha256(work_document),
        "inputs": [
            {
                "collection_id": 17,
                "archive_root_sha256": "1" * 64,
                "content_identity": "2" * 64,
            }
        ],
    }

    assert ProcessingClaimDocument.model_validate(active).state == "active"
    abandoned = {
        **active,
        "state": "abandoned",
        "abandoned_at": "2026-08-24T00:01:00.000000Z",
        "abandonment_reason": "target reported inapplicable",
    }
    assert ProcessingClaimDocument.model_validate(abandoned).state == "abandoned"

    with pytest.raises(ValidationError, match="settlement timestamp"):
        ProcessingClaimDocument.model_validate({**active, "state": "settled"})
    with pytest.raises(ValidationError, match="unsettled claim"):
        ProcessingClaimDocument.model_validate({**active, "output_collection_id": 19})
    with pytest.raises(ValidationError, match="abandonment reason"):
        ProcessingClaimDocument.model_validate(
            {
                **abandoned,
                "abandonment_reason": None,
            }
        )

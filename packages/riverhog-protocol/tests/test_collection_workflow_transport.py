from __future__ import annotations

import pytest
from pydantic import ValidationError
from riverhog_protocol.collection_workflow_transport import (
    CollectionArtifactIdentityDocument,
    CollectionRootIdentityDocument,
    ProcessingClaimCreateDocument,
)
from riverhog_protocol.collection_workflows import (
    CollectionArtifactIdentity,
    CollectionRootIdentity,
    canonical_json_sha256,
)


def test_transport_acceptance_matches_canonical_root_and_artifact_contracts() -> None:
    root = {
        "collection_id": 17,
        "manifest_sha256": "1" * 64,
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
                "manifest_sha256": "1" * 64,
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

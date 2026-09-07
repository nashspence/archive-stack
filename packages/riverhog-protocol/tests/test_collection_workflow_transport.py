from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import ValidationError
from riverhog_protocol.collection_workflow_transport import (
    CONTROLLER_EVIDENCE_MAX_BYTES,
    WORK_DOCUMENT_MAX_BYTES,
    ArtifactDispositionDocument,
    ArtifactDispositionOutputDocument,
    CollectionArtifactIdentityDocument,
    CollectionRootIdentityDocument,
    ProcessingClaimCreateDocument,
    ProcessingClaimDocument,
    ProcessingClaimPlanSealDocument,
    RetirementClaimReferenceDocument,
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
        )


def test_opaque_work_and_evidence_runtime_match_their_schema_byte_bounds() -> None:
    work_schema = ProcessingClaimCreateDocument.model_json_schema()["properties"]["work_document"]
    evidence_schema = ProcessingClaimPlanSealDocument.model_json_schema()["properties"][
        "controller_evidence"
    ]
    assert work_schema["x-riverhog-encoded-bytes-max"] == WORK_DOCUMENT_MAX_BYTES
    assert evidence_schema["x-riverhog-encoded-bytes-max"] == CONTROLLER_EVIDENCE_MAX_BYTES

    work = {"x": "a" * (WORK_DOCUMENT_MAX_BYTES - len(b'{"x":""}'))}
    ProcessingClaimCreateDocument(
        work_id="4" * 64,
        work_document=work,
        work_document_sha256=canonical_json_sha256(work),
    )
    work_above = {"x": work["x"] + "a"}
    with pytest.raises(ValidationError, match="work document exceeds"):
        ProcessingClaimCreateDocument(
            work_id="4" * 64,
            work_document=work_above,
            work_document_sha256=canonical_json_sha256(work_above),
        )

    evidence = {"x": "a" * (CONTROLLER_EVIDENCE_MAX_BYTES - len(b'{"x":""}'))}
    plan = {
        "fence": 1,
        "execution_id": "4" * 64,
        "controller_evidence": evidence,
        "controller_evidence_sha256": canonical_json_sha256(evidence),
        "operation": {"id": "fixture.operation/v1", "sha256": "5" * 64},
        "retirement_policy": "retain",
        "retirement_grace_seconds": 0,
    }
    ProcessingClaimPlanSealDocument.model_validate(plan)
    evidence_above = {"x": evidence["x"] + "a"}
    with pytest.raises(ValidationError, match="controller evidence exceeds"):
        ProcessingClaimPlanSealDocument.model_validate(
            {
                **plan,
                "controller_evidence": evidence_above,
                "controller_evidence_sha256": canonical_json_sha256(evidence_above),
            }
        )


def test_transform_capability_actions_are_the_exact_read_contract() -> None:
    for actions in (["read-inputs"], ["read-inputs", "write-output"]):
        capability = TransformCapabilityCreateDocument(
            fence=1,
            audience="transform:test",
            actions=actions,
        )
        assert capability.actions == actions
        Draft202012Validator(TransformCapabilityCreateDocument.model_json_schema()).validate(
            capability.model_dump(mode="json")
        )

    for actions in (["write-output"], ["read-inputs", "manage-output"]):
        invalid = {
            "fence": 1,
            "audience": "transform:test",
            "actions": actions,
            "ttl_seconds": 900,
        }
        with pytest.raises(ValidationError, match="read-inputs"):
            TransformCapabilityCreateDocument.model_validate(invalid)
        with pytest.raises(JsonSchemaValidationError):
            Draft202012Validator(TransformCapabilityCreateDocument.model_json_schema()).validate(
                invalid
            )


def test_structural_workflow_relationships_match_their_published_schema() -> None:
    input_identity = {
        "collection_id": 17,
        "archive_root_sha256": "1" * 64,
        "path": "camera/clip.mp4",
    }
    transformed = {
        "input": input_identity,
        "status": "transformed",
        "failure": None,
    }
    disposition_validator = Draft202012Validator(ArtifactDispositionDocument.model_json_schema())
    assert ArtifactDispositionDocument.model_validate(transformed).status == "transformed"
    disposition_validator.validate(transformed)

    output = {"input": input_identity, "output_path": "archive/clip.mkv"}
    assert ArtifactDispositionOutputDocument.model_validate(output).output_path == (
        "archive/clip.mkv"
    )

    plan = {
        "fence": 1,
        "execution_id": "4" * 64,
        "controller_evidence": {},
        "controller_evidence_sha256": canonical_json_sha256({}),
        "operation": {"id": "fixture.operation/v1", "sha256": "5" * 64},
        "retirement_policy": "retain",
        "retirement_grace_seconds": 0,
    }
    plan_validator = Draft202012Validator(ProcessingClaimPlanSealDocument.model_json_schema())
    assert ProcessingClaimPlanSealDocument.model_validate(plan).retirement_policy == "retain"
    plan_validator.validate(plan)

    impossible_plan = {**plan, "retirement_grace_seconds": 60}
    with pytest.raises(ValidationError, match="cannot declare retirement grace"):
        ProcessingClaimPlanSealDocument.model_validate(impossible_plan)
    with pytest.raises(JsonSchemaValidationError):
        plan_validator.validate(impossible_plan)


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
        "inputs": {"state": "receiving", "count": 0, "authority": None},
        "outcomes": {"state": "receiving", "count": 0, "authority": None, "failure": None},
    }

    schema_validator = Draft202012Validator(ProcessingClaimDocument.model_json_schema())
    assert ProcessingClaimDocument.model_validate(active).state == "active"
    schema_validator.validate(active)
    abandoned = {
        **active,
        "state": "abandoned",
        "abandoned_at": "2026-08-24T00:01:00.000000Z",
        "abandonment_reason": "target reported inapplicable",
    }
    assert ProcessingClaimDocument.model_validate(abandoned).state == "abandoned"
    schema_validator.validate(abandoned)

    with pytest.raises(ValidationError, match="settlement timestamp"):
        ProcessingClaimDocument.model_validate({**active, "state": "settled"})
    with pytest.raises(JsonSchemaValidationError):
        schema_validator.validate({**active, "state": "settled"})
    with pytest.raises(ValidationError, match="unsettled claim"):
        ProcessingClaimDocument.model_validate({**active, "output_collection_id": 19})
    with pytest.raises(ValidationError, match="abandonment reason"):
        ProcessingClaimDocument.model_validate(
            {
                **abandoned,
                "abandonment_reason": None,
            }
        )


def test_retirement_reference_identifies_one_exact_settlement_form() -> None:
    direct = RetirementClaimReferenceDocument(
        claim_id="1" * 64,
        fence=2,
        work_id="2" * 64,
        execution_id="3" * 64,
        output_collection_id=42,
    )
    delegated = RetirementClaimReferenceDocument(
        claim_id="4" * 64,
        fence=3,
        work_id="5" * 64,
        outcomes={"count": 2, "sha256": "6" * 64},
    )

    assert direct.output_collection_id == 42
    assert delegated.outcomes is not None
    assert delegated.outcomes.sha256 == "6" * 64
    validator = Draft202012Validator(RetirementClaimReferenceDocument.model_json_schema())
    validator.validate(direct.model_dump(mode="json"))
    validator.validate(delegated.model_dump(mode="json"))
    invalid = {
        "claim_id": "1" * 64,
        "fence": 1,
        "work_id": "2" * 64,
    }
    with pytest.raises(ValidationError, match="direct or delegated"):
        RetirementClaimReferenceDocument.model_validate(invalid)
    with pytest.raises(JsonSchemaValidationError):
        validator.validate(invalid)
    with pytest.raises(ValidationError, match="direct or delegated"):
        RetirementClaimReferenceDocument(
            claim_id="1" * 64,
            fence=1,
            work_id="2" * 64,
            execution_id="3" * 64,
            output_collection_id=42,
            outcomes={"count": 2, "sha256": "4" * 64},
        )

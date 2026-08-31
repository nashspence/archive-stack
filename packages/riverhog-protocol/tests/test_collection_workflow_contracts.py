from __future__ import annotations

import hashlib
import json

import pytest
from riverhog_protocol.collection_workflows import (
    ArtifactDisposition,
    ArtifactDispositionSetIdentity,
    CollectionDerivation,
    CollectionRootIdentity,
    OperationIdentity,
    ProducerEvidence,
    RecipeIdentity,
    TransformIntent,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
CONTROLLER_EVIDENCE = {"format": "stove0-controller-evidence/v1", "plan": SHA_A}
CONTROLLER_EVIDENCE_SHA256 = hashlib.sha256(
    json.dumps(CONTROLLER_EVIDENCE, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


def roots() -> tuple[CollectionRootIdentity, ...]:
    return (
        CollectionRootIdentity(11, SHA_A, SHA_B),
        CollectionRootIdentity(12, SHA_B, SHA_C),
    )


def test_transform_identity_is_stable_and_order_independent_at_seal_boundary() -> None:
    recipe = RecipeIdentity("camera-archive/v1", 3, SHA_C)
    operation = OperationIdentity("archive-video/v1", SHA_D)
    first = TransformIntent.seal(
        recipe=recipe,
        operation=operation,
        inputs=reversed(roots()),
        effective_intent={"preserve_audio": True, "quality": 31},
        output_tags=("archive-video", "source-camera"),
        retirement_policy="retain",
    )
    second = TransformIntent.from_mapping(json.loads(first.to_json_bytes()))

    assert second == first
    assert first.transform_id == first.identity_sha256()
    assert first.inputs == roots()


def test_derivation_is_self_contained_and_canonical() -> None:
    recipe = RecipeIdentity("camera-archive/v1", 3, SHA_C)
    operation = OperationIdentity("archive-video/v1", SHA_D)
    intent = TransformIntent.seal(
        recipe=recipe,
        operation=operation,
        inputs=roots(),
        effective_intent={},
        output_tags=("archive-video",),
    )
    derivation = CollectionDerivation(
        execution_id=intent.transform_id,
        claim_id=intent.transform_id,
        fence=4,
        recipe=recipe,
        operation=operation,
        input_set_sha256=SHA_A,
        artifact_set_sha256=SHA_B,
        output_tag_set_sha256=SHA_C,
        execution_envelope_sha256=SHA_A,
        execution_sha256=SHA_B,
        controller_evidence=CONTROLLER_EVIDENCE,
        controller_evidence_sha256=CONTROLLER_EVIDENCE_SHA256,
        disposition_set=ArtifactDispositionSetIdentity(
            disposition_count=2,
            output_edge_count=1,
            output_artifact_count=1,
            sha256=SHA_D,
        ),
    )

    parsed = CollectionDerivation.from_mapping(json.loads(derivation.to_json_bytes()))
    assert parsed == derivation
    assert len(derivation.sha256) == 64


def test_producer_evidence_round_trips_without_mutable_catalog_state() -> None:
    evidence = ProducerEvidence(
        producer_app="ftp-adapter/v1",
        adapter_id="ftp/v1",
        adapter_version="1.0.0",
        source_event_id="camera/upload-0042",
        ingest_source="camera-front",
        source_context={"account": "camera", "remote": "192.0.2.1"},
    )

    assert ProducerEvidence.from_mapping(json.loads(evidence.to_json_bytes())) == evidence
    assert len(evidence.sha256) == 64


def test_successful_disposition_cannot_hide_an_omission() -> None:
    with pytest.raises(ValueError, match="cannot carry failure"):
        ArtifactDisposition(
            11,
            SHA_A,
            "camera/a.mov",
            "preserved",
            code="ignored",
            message="not allowed",
        )


def test_derivation_binds_the_exact_sealed_authorities() -> None:
    recipe = RecipeIdentity("camera-archive/v1", 3, SHA_C)
    operation = OperationIdentity("archive-video/v1", SHA_D)
    intent = TransformIntent.seal(
        recipe=recipe,
        operation=operation,
        inputs=roots(),
        effective_intent={},
        output_tags=("archive-video",),
    )
    derivation = CollectionDerivation(
        execution_id=intent.transform_id,
        claim_id=intent.transform_id,
        fence=4,
        recipe=recipe,
        operation=operation,
        input_set_sha256=SHA_A,
        artifact_set_sha256=SHA_B,
        output_tag_set_sha256=SHA_C,
        execution_envelope_sha256=SHA_A,
        execution_sha256=SHA_B,
        controller_evidence=CONTROLLER_EVIDENCE,
        controller_evidence_sha256=CONTROLLER_EVIDENCE_SHA256,
        disposition_set=ArtifactDispositionSetIdentity(2, 1, 1, SHA_D),
    )

    assert derivation.input_set_sha256 == SHA_A
    assert derivation.artifact_set_sha256 == SHA_B
    assert derivation.output_tag_set_sha256 == SHA_C
    assert derivation.disposition_set.sha256 == SHA_D

from __future__ import annotations

import pytest
from stove0_review_target_contracts import (
    REVIEW_MATERIALIZE_INTENT_CONFORMANCE_VECTORS,
    REVIEW_MATERIALIZE_INTENT_SCHEMA,
    REVIEW_MATERIALIZE_INTENT_SEMANTICS,
    REVIEW_MATERIALIZE_OPERATION,
    REVIEW_RCLONE_DELIVER_OPERATION,
    ReviewMaterializeIntent,
    ReviewSamplePlan,
    ReviewSamplePlanPayload,
    ReviewSampleWindow,
    validate_review_materialize_intent,
)


def test_review_target_contracts_retain_exact_result_and_retirement_semantics() -> None:
    plan = ReviewSamplePlan.seal(
        ReviewSamplePlanPayload(
            samples_per_artifact=1,
            window_duration_ms=1_000,
            windows=(
                ReviewSampleWindow(
                    artifact_id="camera",
                    start_ms=0,
                    duration_ms=1_000,
                ),
            ),
        )
    )

    assert REVIEW_MATERIALIZE_OPERATION.id == "stove0.review.materialize/v1"
    assert (
        REVIEW_MATERIALIZE_OPERATION.contract_sha256
        == "4be8075379efc3b1cc7ff30f3458ad895e855e0d18357b5caf39d7997f4ec3f4"
    )
    assert REVIEW_MATERIALIZE_OPERATION.result_kind == "collection"
    assert REVIEW_RCLONE_DELIVER_OPERATION.id == "stove0.review.rclone-deliver/v1"
    assert (
        REVIEW_RCLONE_DELIVER_OPERATION.contract_sha256
        == "6295e22ff6cd95f1c5657d427ebb47622f3625112c831ef0e51c366bfaf38ed2"
    )
    assert REVIEW_RCLONE_DELIVER_OPERATION.result_kind == "external-effect"
    assert REVIEW_MATERIALIZE_OPERATION.source_retirement_permitted is False
    assert REVIEW_RCLONE_DELIVER_OPERATION.source_retirement_permitted is False
    assert len(plan.sample_plan_sha256) == 64
    intent = {
        "sample_plan": plan.model_dump(mode="json"),
        "variant": {"id": "opus-96", "portable_intent": {"bitrate_kbps": 96}},
    }
    assert ReviewMaterializeIntent.model_validate(intent).sample_plan == plan
    assert REVIEW_MATERIALIZE_INTENT_SCHEMA.document == ReviewMaterializeIntent.model_json_schema()
    assert REVIEW_MATERIALIZE_OPERATION.intent_semantics == REVIEW_MATERIALIZE_INTENT_SEMANTICS
    assert REVIEW_RCLONE_DELIVER_OPERATION.intent_semantics == REVIEW_MATERIALIZE_INTENT_SEMANTICS
    validate_review_materialize_intent(intent)


def test_review_sample_plan_enforces_its_exact_declared_shape() -> None:
    windows = (
        ReviewSampleWindow(artifact_id="camera", start_ms=0, duration_ms=1_000),
        ReviewSampleWindow(artifact_id="camera", start_ms=2_000, duration_ms=1_000),
        ReviewSampleWindow(artifact_id="microphone", start_ms=0, duration_ms=1_000),
        ReviewSampleWindow(artifact_id="microphone", start_ms=2_000, duration_ms=1_000),
    )
    payload = ReviewSamplePlanPayload(
        samples_per_artifact=2,
        window_duration_ms=1_000,
        windows=windows,
    )
    assert ReviewSamplePlan.seal(payload).windows == windows

    with pytest.raises(ValueError, match="duration differs"):
        ReviewSamplePlanPayload(
            samples_per_artifact=2,
            window_duration_ms=500,
            windows=windows,
        )
    with pytest.raises(ValueError, match="per-artifact declaration"):
        ReviewSamplePlanPayload(
            samples_per_artifact=1,
            window_duration_ms=1_000,
            windows=windows,
        )
    with pytest.raises(ValueError, match="canonically ordered"):
        ReviewSamplePlanPayload(
            samples_per_artifact=2,
            window_duration_ms=1_000,
            windows=tuple(reversed(windows)),
        )


def test_review_materialize_semantic_vectors_are_bound_and_executable() -> None:
    assert REVIEW_MATERIALIZE_INTENT_CONFORMANCE_VECTORS.profile_id == (
        REVIEW_MATERIALIZE_INTENT_SEMANTICS.id
    )
    assert REVIEW_MATERIALIZE_INTENT_SEMANTICS.conformance_vectors_sha256 == (
        REVIEW_MATERIALIZE_INTENT_CONFORMANCE_VECTORS.sha256
    )
    for vector in REVIEW_MATERIALIZE_INTENT_CONFORMANCE_VECTORS.vectors:
        if vector.accepted:
            validate_review_materialize_intent(vector.intent)
        else:
            with pytest.raises(ValueError):
                validate_review_materialize_intent(vector.intent)

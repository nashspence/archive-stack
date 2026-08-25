from __future__ import annotations

import pytest
from stove0_review_target_contracts import (
    REVIEW_MATERIALIZE_INTENT_SCHEMA,
    REVIEW_MATERIALIZE_OPERATION,
    REVIEW_RCLONE_DELIVER_OPERATION,
    ReviewMaterializeIntent,
    ReviewSamplePlan,
    ReviewSamplePlanPayload,
    ReviewSampleWindow,
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
        == "ff0157d80d1bf97e73c983ae6edb64da86570305f41ade031fda4671e0dd2532"
    )
    assert REVIEW_MATERIALIZE_OPERATION.result_kind == "collection"
    assert REVIEW_RCLONE_DELIVER_OPERATION.id == "stove0.review.rclone-deliver/v1"
    assert (
        REVIEW_RCLONE_DELIVER_OPERATION.contract_sha256
        == "df33cea0d93fd8ecfff758bfeb952cb923997acbb03fec5f89908067abd35cf7"
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

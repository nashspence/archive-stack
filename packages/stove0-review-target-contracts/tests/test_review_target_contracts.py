from __future__ import annotations

from stove0_review_target_contracts import (
    REVIEW_MATERIALIZE_OPERATION,
    REVIEW_RCLONE_DELIVER_OPERATION,
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
        == "bebe78213213ede359338aa1b4eadde2c66646a53250e443d395cddb8cdd991c"
    )
    assert REVIEW_MATERIALIZE_OPERATION.result_kind == "collection"
    assert REVIEW_RCLONE_DELIVER_OPERATION.id == "stove0.review.rclone-deliver/v1"
    assert (
        REVIEW_RCLONE_DELIVER_OPERATION.contract_sha256
        == "7ff2c8c93f9ea1c06019a987de31242b1c72b5108ba646cf1a829b1b678a9f71"
    )
    assert REVIEW_RCLONE_DELIVER_OPERATION.result_kind == "external-effect"
    assert REVIEW_MATERIALIZE_OPERATION.source_retirement_permitted is False
    assert REVIEW_RCLONE_DELIVER_OPERATION.source_retirement_permitted is False
    assert len(plan.sample_plan_sha256) == 64

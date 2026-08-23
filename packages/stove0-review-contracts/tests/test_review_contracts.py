from __future__ import annotations

import subprocess
import sys

import pytest
from pydantic import ValidationError
from stove0_protocol import CollectionRootRef, RecipeRef
from stove0_review_contracts import (
    MEDIA_SAMPLING_OBSERVER_CONTRACT,
    REVIEW_MATERIALIZE_OPERATION,
    MediaSamplingArtifactFacts,
    MediaSamplingFacts,
    ReviewVariant,
    SampleableRange,
    contract_report,
    evenly_spaced_sample_plan,
    review_evaluation_definition,
)


def _sha(character: str) -> str:
    return character * 64


def _facts() -> MediaSamplingFacts:
    return MediaSamplingFacts(
        artifacts=(
            MediaSamplingArtifactFacts(
                artifact_id="camera-a",
                duration_ms=120_000,
                sampleable_ranges=(
                    SampleableRange(start_ms=0, duration_ms=50_000),
                    SampleableRange(start_ms=60_000, duration_ms=60_000),
                ),
            ),
        )
    )


def test_review_contract_pack_is_content_specific_but_core_independent() -> None:
    assert MEDIA_SAMPLING_OBSERVER_CONTRACT.id == "stove0.review.media-sampling/v1"
    assert REVIEW_MATERIALIZE_OPERATION.id == "stove0.review.materialize/v1"
    assert REVIEW_MATERIALIZE_OPERATION.source_retirement_permitted is False
    assert contract_report()["status"] == "conformant"

    command = [
        sys.executable,
        "-c",
        (
            "import sys; import stove0_review_contracts; "
            "forbidden = {'stove0_core', 'stove0_observer_support', "
            "'stove0_target_support', 'riverhog_api_client', "
            "'riverhog_transform_sdk'}; "
            "loaded = forbidden & set(sys.modules); "
            "assert not loaded, sorted(loaded)"
        ),
    ]
    subprocess.run(command, check=True)


def test_evenly_spaced_sample_plan_is_deterministic_within_sampleable_domains() -> None:
    first = evenly_spaced_sample_plan(
        _facts(),
        samples_per_artifact=4,
        window_duration_ms=5_000,
    )
    second = evenly_spaced_sample_plan(
        _facts(),
        samples_per_artifact=4,
        window_duration_ms=5_000,
    )
    assert first == second
    assert len(first.windows) == 4
    assert all(item.duration_ms == 5_000 for item in first.windows)
    assert len({item.start_ms for item in first.windows}) == 4
    assert first.windows == tuple(sorted(first.windows, key=lambda item: item.start_ms))

    with pytest.raises(ValueError, match="insufficient"):
        evenly_spaced_sample_plan(
            _facts(),
            samples_per_artifact=64,
            window_duration_ms=119_999,
        )


def test_review_work_cardinality_has_no_protocol_ceiling() -> None:
    source = REVIEW_MATERIALIZE_OPERATION.inputs[0]
    sampled_outputs = tuple(
        output
        for output in REVIEW_MATERIALIZE_OPERATION.outputs
        if output.role != "stove0.review.index/v1"
    )
    facts_schema = MEDIA_SAMPLING_OBSERVER_CONTRACT.facts_schema.document
    plan = evenly_spaced_sample_plan(
        MediaSamplingFacts(
            artifacts=tuple(
                MediaSamplingArtifactFacts(
                    artifact_id=f"camera-{index:03d}",
                    duration_ms=1_000,
                    sampleable_ranges=(SampleableRange(start_ms=0, duration_ms=1_000),),
                )
                for index in range(257)
            )
        ),
        samples_per_artifact=1,
        window_duration_ms=100,
    )
    long_window_plan = evenly_spaced_sample_plan(
        MediaSamplingFacts(
            artifacts=(
                MediaSamplingArtifactFacts(
                    artifact_id="camera-long",
                    duration_ms=3 * 60 * 60 * 1000,
                    sampleable_ranges=(
                        SampleableRange(start_ms=0, duration_ms=3 * 60 * 60 * 1000),
                    ),
                ),
            )
        ),
        samples_per_artifact=1,
        window_duration_ms=2 * 60 * 60 * 1000,
    )

    assert source.maximum is None
    assert all(output.maximum is None for output in sampled_outputs)
    assert facts_schema["properties"]["artifacts"].get("maxItems") is None
    assert len(plan.windows) == 257
    assert long_window_plan.windows[0].duration_ms == 7_200_000


def test_review_evaluation_expands_one_normal_work_per_variant() -> None:
    sample_plan = evenly_spaced_sample_plan(
        _facts(),
        samples_per_artifact=3,
        window_duration_ms=4_000,
    )
    definition = review_evaluation_definition(
        recipe=RecipeRef(id="review.encode/v1", revision=1, sha256=_sha("a")),
        inputs=(
            CollectionRootRef(
                collection_id=1,
                manifest_sha256=_sha("b"),
                content_identity=_sha("c"),
            ),
        ),
        sample_plan=sample_plan,
        variants=(
            ReviewVariant(id="quality-24", portable_intent={"quality": 24}),
            ReviewVariant(id="quality-28", portable_intent={"quality": 28}),
            ReviewVariant(id="quality-32", portable_intent={"quality": 32}),
        ),
    )
    children = definition.child_works()
    assert len(children) == 3
    assert len({item.work_id for item in children}) == 3
    assert {item.evaluation.variant_id for item in children if item.evaluation is not None} == {
        "quality-24",
        "quality-28",
        "quality-32",
    }
    assert {item.evaluation.matrix_sha256 for item in children if item.evaluation is not None} == {
        definition.matrix.matrix_sha256
    }
    assert all(
        item.effective_intent["review_sample_plan"]["sample_plan_sha256"]
        == sample_plan.sample_plan_sha256
        for item in children
    )


def test_materialized_trial_requires_exactly_one_variant() -> None:
    sample_plan = evenly_spaced_sample_plan(
        _facts(),
        samples_per_artifact=1,
        window_duration_ms=4_000,
    )
    with pytest.raises(ValidationError, match="exactly one variant"):
        review_evaluation_definition(
            recipe=RecipeRef(id="review.encode/v1", revision=1, sha256=_sha("a")),
            inputs=(
                CollectionRootRef(
                    collection_id=1,
                    manifest_sha256=_sha("b"),
                    content_identity=_sha("c"),
                ),
            ),
            sample_plan=sample_plan,
            variants=(
                ReviewVariant(id="one"),
                ReviewVariant(id="two"),
            ),
            purpose="trial",
        )

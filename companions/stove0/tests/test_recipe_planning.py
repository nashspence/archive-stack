from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from riverhog_api_client import ApiClient
from riverhog_protocol.collection_workflows import canonical_json_sha256
from stove0_core import ObserverPort, RecipeCatalog, RecipeDefinition, RecipePlanner, TargetPort
from stove0_core.recipes import ArtifactRule, RecipeRoute
from stove0_media_contracts import (
    AUDIO_ARCHIVE_OPERATION,
    PRESERVE_OPERATION,
    VIDEO_ARCHIVE_OPERATION,
)
from stove0_protocol import CollectionRootRef, JsonSchemaDocument
from stove0_review_contracts import (
    REVIEW_SAMPLE_ENCODE_OPERATION,
    REVIEW_SAMPLE_ENCODE_OPERATION_ID,
    REVIEW_SOURCE_ROLE,
    ReviewSamplePlan,
    ReviewSamplePlanPayload,
    ReviewSampleWindow,
    ReviewVariant,
    review_evaluation_definition,
    review_operation_intent,
    review_target_options,
)
from stove0_target_support import TargetContract, TargetContractPayload, TargetOperationSupport


def _sha(character: str) -> str:
    return character * 64


class CatalogApi:
    def get_collection(self, collection_id: int) -> dict[str, object]:
        assert collection_id == 11
        return {
            "id": 11,
            "manifest_sha256": _sha("1"),
            "content_etag": _sha("2"),
        }

    def search(self, **_kwargs: object) -> dict[str, object]:
        return {
            "files": [
                {
                    "path": "camera/source.mp4",
                    "bytes": 100,
                    "sha256": _sha("3"),
                }
            ]
        }


class Targets:
    def __init__(self, contract: TargetContract) -> None:
        self._contract = contract

    def contract(self, registration_id: str) -> TargetContract:
        assert registration_id == "review-ffmpeg"
        return self._contract


def test_review_compiler_binds_semantic_intent_and_options_before_preflight() -> None:
    target = TargetContract.seal(
        TargetContractPayload(
            implementation_id="riverhog.review-ffmpeg/v1",
            implementation_version="1.0.0",
            source_revision="fixture",
            operations=(
                TargetOperationSupport(
                    operation_id=REVIEW_SAMPLE_ENCODE_OPERATION.id,
                    operation_contract_sha256=(REVIEW_SAMPLE_ENCODE_OPERATION.contract_sha256),
                    options_schema=JsonSchemaDocument.from_schema(
                        "riverhog.review-ffmpeg-options/v1",
                        {
                            "type": "object",
                            "properties": {
                                "threads": {"type": "integer"},
                                "crf": {"type": "integer"},
                            },
                            "additionalProperties": False,
                        },
                    ),
                ),
            ),
        )
    )
    recipe = RecipeDefinition(
        id="review-evaluation/v1",
        revision=1,
        input_tags=("review-source",),
        routes=(
            RecipeRoute(
                id="review",
                operation_id=REVIEW_SAMPLE_ENCODE_OPERATION.id,
                target_registration_id="review-ffmpeg",
                artifact_rules=(
                    ArtifactRule(
                        glob="*.mp4",
                        role=REVIEW_SOURCE_ROLE,
                        media_type="video/mp4",
                    ),
                ),
                target_options={"threads": 2},
                output_tags=("review-output",),
            ),
        ),
    )
    assert recipe.event_input_closure == "single-finalized-collection"
    catalog = RecipeCatalog(
        operations=(REVIEW_SAMPLE_ENCODE_OPERATION,),
        recipes=(recipe,),
    )
    root = CollectionRootRef(
        collection_id=11,
        manifest_sha256=_sha("1"),
        content_etag=_sha("2"),
    )
    artifact_id = (
        "a-" + canonical_json_sha256({"collection_id": 11, "path": "camera/source.mp4"})[:32]
    )
    sample_plan = ReviewSamplePlan.seal(
        ReviewSamplePlanPayload(
            samples_per_artifact=1,
            window_duration_ms=500,
            windows=(
                ReviewSampleWindow(
                    artifact_id=artifact_id,
                    start_ms=100,
                    duration_ms=500,
                ),
            ),
        )
    )
    definition = review_evaluation_definition(
        recipe=recipe.ref,
        inputs=(root,),
        sample_plan=sample_plan,
        variants=(
            ReviewVariant(
                id="crf-30",
                portable_intent={"label": "candidate"},
                target_options={"crf": 30},
            ),
        ),
    )
    planner = RecipePlanner(
        catalog=catalog,
        riverhog=cast(ApiClient, CatalogApi()),
        observers=cast(ObserverPort, object()),
        targets=cast(TargetPort, Targets(target)),
        operation_compilers={
            REVIEW_SAMPLE_ENCODE_OPERATION_ID: lambda work: (
                review_operation_intent(work),
                review_target_options(work),
            )
        },
    )

    work = definition.child_work("crf-30")
    plan = planner.workflow_plan(work, ())
    assert not hasattr(plan, "code")
    request = planner.target_preflight_request(cast(Any, plan))

    assert plan.requested_target_options == {"threads": 2, "crf": 30}
    assert plan.input_retrieval_policy == "available-only"
    assert request.target_options == plan.requested_target_options
    assert request.intent == {
        "sample_plan": sample_plan.model_dump(mode="json"),
        "variant": {
            "id": "crf-30",
            "portable_intent": {"label": "candidate"},
        },
    }
    assert "review_variant" not in request.intent


def test_reference_recipes_embed_exact_maintained_contracts_and_explicit_cost_policy() -> None:
    path = Path(__file__).parents[3] / "companions/stove0/config/recipes.example.yaml"
    catalog = RecipeCatalog.load(path)

    assert catalog.operations == tuple(
        sorted(
            (
                AUDIO_ARCHIVE_OPERATION,
                PRESERVE_OPERATION,
                VIDEO_ARCHIVE_OPERATION,
                REVIEW_SAMPLE_ENCODE_OPERATION,
            ),
            key=lambda operation: operation.id,
        )
    )
    assert {recipe.event_input_closure for recipe in catalog.recipes} == {
        "single-finalized-collection"
    }
    policies = {
        route.input_retrieval_policy for recipe in catalog.recipes for route in recipe.routes
    }
    assert policies == {"allow", "available-only"}

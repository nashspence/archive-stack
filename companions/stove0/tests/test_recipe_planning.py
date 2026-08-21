from __future__ import annotations

from pathlib import Path
from typing import cast

from riverhog_api_client import ApiClient
from riverhog_protocol.collection_workflows import canonical_json_sha256
from stove0_core import ObserverPort, RecipeCatalog, RecipeDefinition, RecipePlanner, TargetPort
from stove0_core.recipes import (
    ArtifactRule,
    OperationProjection,
    RecipeJoin,
    RecipeJoinMember,
    RecipeRoute,
)
from stove0_media_archive_contracts import (
    AUDIO_ARCHIVE_OPERATION,
    AV1_OPUS_ARCHIVE_OPERATION,
)
from stove0_protocol import (
    ArtifactSelection,
    ArtifactSubject,
    BranchSetDecision,
    BranchSettlement,
    CollectionRootRef,
    JsonSchemaDocument,
    resolve_join_plan,
)
from stove0_review_contracts import (
    REVIEW_MATERIALIZE_OPERATION,
    REVIEW_SOURCE_ROLE,
    ReviewSamplePlan,
    ReviewSamplePlanPayload,
    ReviewSampleWindow,
    ReviewVariant,
    review_evaluation_definition,
)
from stove0_target_support import (
    InputArtifactContract,
    OperationContract,
    OperationContractPayload,
    OutputArtifactContract,
    TargetContract,
    TargetContractPayload,
    TargetOperationSupport,
)


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


def test_review_recipe_projects_semantic_intent_and_options_before_preflight() -> None:
    target = TargetContract.seal(
        TargetContractPayload(
            implementation_id="riverhog.review-ffmpeg/v1",
            implementation_version="1.0.0",
            source_revision="fixture",
            image_digest=_sha("9"),
            operations=(
                TargetOperationSupport(
                    operation_id=REVIEW_MATERIALIZE_OPERATION.id,
                    operation_contract_sha256=(REVIEW_MATERIALIZE_OPERATION.contract_sha256),
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
                operation_id=REVIEW_MATERIALIZE_OPERATION.id,
                target_registration_id="review-ffmpeg",
                artifact_rules=(
                    ArtifactRule(
                        glob="*.mp4",
                        role=REVIEW_SOURCE_ROLE,
                        media_type="video/mp4",
                    ),
                ),
                target_options={"threads": 2},
                projections=(
                    OperationProjection(
                        source="work-effective-intent",
                        source_pointer="/review_sample_plan",
                        destination="intent",
                        destination_pointer="/sample_plan",
                    ),
                    OperationProjection(
                        source="work-evaluation",
                        source_pointer="/variant_id",
                        destination="intent",
                        destination_pointer="/variant/id",
                    ),
                    OperationProjection(
                        source="work-evaluation",
                        source_pointer="/parameters/review_variant/portable_intent",
                        destination="intent",
                        destination_pointer="/variant/portable_intent",
                    ),
                    OperationProjection(
                        source="work-evaluation",
                        source_pointer="/parameters/review_variant/target_options",
                        destination="target-options",
                        destination_pointer="",
                    ),
                ),
                output_tags=("review-output",),
            ),
        ),
    )
    assert recipe.event_input_closure == "single-finalized-collection"
    catalog = RecipeCatalog(
        operations=(REVIEW_MATERIALIZE_OPERATION,),
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
    )

    work = definition.child_work("crf-30")
    decision = planner.workflow_plan(work, ())
    assert isinstance(decision, BranchSetDecision)
    assert len(decision.plan.branches) == 1
    plan = decision.plan.branches[0].workflow_plan
    request = planner.target_preflight_request(plan, decision.selection_documents)

    assert plan.requested_target_options == {"threads": 2, "crf": 30}
    assert plan.work.evaluation == work.evaluation
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
                AV1_OPUS_ARCHIVE_OPERATION,
                REVIEW_MATERIALIZE_OPERATION,
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


def test_production_planner_resolves_overlapping_branches_into_one_exact_join() -> None:
    empty_schema = JsonSchemaDocument.from_schema(
        "fixture.empty/v1",
        {"type": "object", "additionalProperties": False},
    )
    branch_operation = OperationContract.seal(
        OperationContractPayload(
            id="fixture.branch/v1",
            intent_schema=empty_schema,
            inputs=(InputArtifactContract(role="fixture.source/v1"),),
            outputs=(
                OutputArtifactContract(
                    role="fixture.branch-output/v1",
                    derived_from_roles=("fixture.source/v1",),
                ),
            ),
        )
    )
    join_operation = OperationContract.seal(
        OperationContractPayload(
            id="fixture.join/v1",
            intent_schema=empty_schema,
            inputs=(InputArtifactContract(role="fixture.branch-output/v1", minimum=2),),
            outputs=(
                OutputArtifactContract(
                    role="fixture.join-output/v1",
                    derived_from_roles=("fixture.branch-output/v1",),
                ),
            ),
        )
    )
    target = TargetContract.seal(
        TargetContractPayload(
            implementation_id="fixture.target/v1",
            implementation_version="1.0.0",
            source_revision="fixture",
            image_digest=_sha("9"),
            operations=tuple(
                TargetOperationSupport(
                    operation_id=operation.id,
                    operation_contract_sha256=operation.contract_sha256,
                    options_schema=empty_schema,
                )
                for operation in (branch_operation, join_operation)
            ),
        )
    )

    class ForkJoinTargets:
        def contract(self, registration_id: str) -> TargetContract:
            assert registration_id == "fixture-target"
            return target

    recipe = RecipeDefinition(
        id="fixture.fork-join/v1",
        revision=1,
        input_tags=("fixture",),
        routes=tuple(
            RecipeRoute(
                id=branch_id,
                operation_id=branch_operation.id,
                target_registration_id="fixture-target",
                artifact_rules=(ArtifactRule(role="fixture.source/v1"),),
                output_tags=(f"fixture-{branch_id}",),
            )
            for branch_id in ("audio", "video")
        ),
        join=RecipeJoin(
            id="combine",
            members=tuple(
                RecipeJoinMember(
                    branch_id=branch_id,
                    output_roles=("fixture.branch-output/v1",),
                )
                for branch_id in ("audio", "video")
            ),
            operation_id=join_operation.id,
            target_registration_id="fixture-target",
            output_tags=("fixture-joined",),
        ),
    )
    planner = RecipePlanner(
        catalog=RecipeCatalog(
            operations=(branch_operation, join_operation),
            recipes=(recipe,),
        ),
        riverhog=cast(ApiClient, CatalogApi()),
        observers=cast(ObserverPort, object()),
        targets=cast(TargetPort, ForkJoinTargets()),
    )
    root = CollectionRootRef(
        collection_id=11,
        manifest_sha256=_sha("1"),
        content_etag=_sha("2"),
    )
    work = planner.create_work(recipe.id, (root,))
    decision = planner.workflow_plan(work, ())
    assert isinstance(decision, BranchSetDecision)
    assert decision.plan.join is not None
    branch_selections = [
        decision.selection_documents[item.artifact_selection.selection_sha256]
        for item in decision.plan.branches
    ]
    assert branch_selections[0] == branch_selections[1]

    selections = dict(decision.selection_documents)
    settlements: list[BranchSettlement] = []
    for collection_id, branch in enumerate(decision.plan.branches, start=21):
        output_root = CollectionRootRef(
            collection_id=collection_id,
            manifest_sha256=f"{collection_id % 16:x}" * 64,
            content_etag=f"{(collection_id + 1) % 16:x}" * 64,
        )
        output = ArtifactSelection.seal(
            (
                ArtifactSubject(
                    id=f"{branch.branch_id}-output",
                    role="fixture.branch-output/v1",
                    collection=output_root,
                    path=f"{branch.branch_id}/output.bin",
                    bytes=12,
                    sha256=f"{(collection_id + 2) % 16:x}" * 64,
                ),
            )
        )
        selections[output.selection_sha256] = output
        settlements.append(
            BranchSettlement.seal(
                branch=branch,
                derivation_sha256=f"{(collection_id + 3) % 16:x}" * 64,
                output_collection=output_root,
                output_selection=output,
            )
        )
    resolved = resolve_join_plan(decision.plan, selections, settlements)
    assert resolved is not None
    join_plan, join_selections = resolved
    request = planner.target_preflight_request(
        join_plan.workflow_plan,
        {item.selection_sha256: item for item in join_selections},
    )
    assert len(request.inputs) == 2
    assert len({item.id for item in request.inputs}) == 2
    assert {item.collection.collection_id for item in request.inputs} == {21, 22}

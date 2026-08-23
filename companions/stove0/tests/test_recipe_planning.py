from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from riverhog_api_client import ApiClient
from riverhog_protocol.collection_workflows import canonical_json_sha256
from stove0_core import (
    ObserverPort,
    RecipeCatalog,
    RecipeDefinition,
    RecipePlanner,
    TargetPort,
    WorkInapplicable,
)
from stove0_core.recipes import (
    ArtifactRule,
    FactPredicate,
    ObserverUse,
    OperationProjection,
    RecipeJoin,
    RecipeJoinMember,
    RecipeRoute,
)
from stove0_media_archive_contracts import (
    AUDIO_ARCHIVE_OPERATION,
    AV1_OPUS_ARCHIVE_OPERATION,
    MEDIA_METADATA_OBSERVER_CONTRACT,
    SOURCE_ROLE,
    XMP_SOURCE_ROLE,
    MediaArtifactFacts,
    MediaFactEvidence,
    MediaMetadataFact,
    MediaMetadataFacts,
)
from stove0_observer_support import ObservationResultBuilder
from stove0_protocol import (
    ArtifactSelection,
    ArtifactSubject,
    BranchSetDecision,
    BranchSettlement,
    CollectionRootRef,
    JsonSchemaDocument,
    ObservationEvidence,
    ObserverContractSupport,
    ObserverDescriptor,
    ObserverDescriptorPayload,
    resolve_join_plan,
)
from stove0_review_contracts import (
    REVIEW_MATERIALIZE_OPERATION,
    REVIEW_RCLONE_DELIVER_OPERATION,
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
            "content_identity": _sha("2"),
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


class MediaObservers:
    def __init__(self) -> None:
        self.value = ObserverDescriptor.seal(
            ObserverDescriptorPayload(
                implementation_id="fixture.exiftool-observer/v1",
                implementation_version="1.0.0",
                source_revision="fixture",
                image_digest=_sha("9"),
                contracts=(
                    ObserverContractSupport.from_contract(
                        MEDIA_METADATA_OBSERVER_CONTRACT,
                        preferred_subject_batch_size=100,
                    ),
                ),
            )
        )

    def descriptor(self, registration_id: str) -> ObserverDescriptor:
        assert registration_id == "exiftool"
        return self.value


class ArchiveTargets:
    def __init__(self) -> None:
        options = JsonSchemaDocument.from_schema(
            "fixture.archive-options/v1",
            {"type": "object", "additionalProperties": False},
        )
        self.value = TargetContract.seal(
            TargetContractPayload(
                implementation_id="fixture.archive-target/v1",
                implementation_version="1.0.0",
                source_revision="fixture",
                image_digest=_sha("8"),
                operations=(
                    TargetOperationSupport(
                        operation_id=AUDIO_ARCHIVE_OPERATION.id,
                        operation_contract_sha256=AUDIO_ARCHIVE_OPERATION.contract_sha256,
                        options_schema=options,
                    ),
                ),
            )
        )

    def contract(self, registration_id: str) -> TargetContract:
        assert registration_id in {"opus", "fixture-target"}
        return self.value


class LargeCatalogApi(CatalogApi):
    def search(self, **_kwargs: object) -> dict[str, object]:
        return {
            "files": [
                {
                    "path": f"camera/item-{index:03d}.xmp",
                    "bytes": 100 + index,
                    "sha256": f"{index % 16:x}" * 64,
                }
                for index in range(257)
            ]
        }


def test_recipe_projection_count_is_defined_by_the_recipe() -> None:
    projections = tuple(
        OperationProjection(
            source="work-effective-intent",
            source_pointer=f"/source-{index:03}",
            destination="intent",
            destination_pointer=f"/destination-{index:03}",
        )
        for index in range(65)
    )

    route = RecipeRoute(
        id="large-explicit-projection",
        operation_id="fixture.operation/v1",
        target_registration_id="fixture-target",
        projections=projections,
        output_tags=("fixture-output",),
    )

    assert route.projections == projections


def test_observer_preference_batches_unbounded_collection_work_without_omission() -> None:
    recipe = RecipeDefinition(
        id="fixture.large-observation/v1",
        revision=1,
        input_tags=("fixture",),
        observers=(
            ObserverUse(
                registration_id="exiftool",
                contract_id=MEDIA_METADATA_OBSERVER_CONTRACT.id,
                artifact_rules=(ArtifactRule(role="stove0.media.source/v1"),),
            ),
        ),
        routes=(
            RecipeRoute(
                id="archive",
                operation_id=AUDIO_ARCHIVE_OPERATION.id,
                target_registration_id="opus",
                artifact_rules=(ArtifactRule(role="stove0.media.source/v1"),),
                output_tags=("archive",),
            ),
            RecipeRoute(
                id="incorrect-absence",
                when=(
                    FactPredicate(
                        observation_contract_id=MEDIA_METADATA_OBSERVER_CONTRACT.id,
                        pointer="/artifacts/63/state",
                        operator="exists",
                        value=False,
                    ),
                ),
                operation_id=AUDIO_ARCHIVE_OPERATION.id,
                target_registration_id="opus",
                artifact_rules=(ArtifactRule(role="stove0.media.source/v1"),),
                output_tags=("incorrect",),
            ),
            RecipeRoute(
                id="observed-unsupported",
                when=(
                    FactPredicate(
                        observation_contract_id=MEDIA_METADATA_OBSERVER_CONTRACT.id,
                        pointer="/artifacts/0/state",
                        operator="equals",
                        value="unsupported",
                    ),
                ),
                operation_id=AUDIO_ARCHIVE_OPERATION.id,
                target_registration_id="opus",
                artifact_rules=(ArtifactRule(role="stove0.media.source/v1"),),
                output_tags=("observed",),
            ),
        ),
    )
    observers = MediaObservers()
    planner = RecipePlanner(
        catalog=RecipeCatalog(
            operations=(AUDIO_ARCHIVE_OPERATION,),
            recipes=(recipe,),
        ),
        riverhog=cast(ApiClient, LargeCatalogApi()),
        observers=cast(ObserverPort, observers),
        targets=cast(TargetPort, ArchiveTargets()),
    )
    root = CollectionRootRef(
        collection_id=11,
        manifest_sha256=_sha("1"),
        content_identity=_sha("2"),
    )
    work = planner.create_work(recipe.id, (root,))

    requests = planner.observation_requests(work)

    assert sorted(len(request.subjects) for request in requests) == [57, 100, 100]
    subjects = [subject for request in requests for subject in request.subjects]
    assert len(subjects) == 257
    assert len({subject.id for subject in subjects}) == 257
    evidence = tuple(
        ObservationEvidence(
            request=request,
            result=ObservationResultBuilder(observers.value, request).observed(
                MediaMetadataFacts(
                    artifacts=tuple(
                        MediaArtifactFacts(artifact_id=subject.id, state="unsupported")
                        for subject in request.subjects
                    )
                ).model_dump(mode="json")
            ),
        )
        for request in requests
    )

    decision = planner.workflow_plan(work, evidence)

    assert isinstance(decision, BranchSetDecision)
    assert decision.plan.evidence_sha256s == tuple(
        sorted(item.result.result_sha256 for item in evidence)
    )
    assert [branch.branch_id for branch in decision.plan.branches] == [
        "archive",
        "observed-unsupported",
    ]
    assert all(branch.workflow_plan.observations == evidence for branch in decision.plan.branches)


class AssociatedMediaCatalogApi(CatalogApi):
    def search(self, **_kwargs: object) -> dict[str, object]:
        return {
            "files": [
                {"path": "camera/clip.mov", "bytes": 100, "sha256": _sha("3")},
                {"path": "camera/clip.xmp", "bytes": 20, "sha256": _sha("4")},
                {"path": "camera/retained.txt", "bytes": 10, "sha256": _sha("5")},
            ]
        }


def test_media_observation_evidence_binds_exact_primary_sidecar_selection() -> None:
    media_rules = (
        ArtifactRule(
            glob="camera/*.xmp",
            role=XMP_SOURCE_ROLE,
            media_type="application/rdf+xml",
        ),
        ArtifactRule(glob="camera/*.mov", role=SOURCE_ROLE, media_type="video/quicktime"),
    )
    recipe = RecipeDefinition(
        id="fixture.observed-media/v1",
        revision=1,
        input_tags=("fixture",),
        observers=(
            ObserverUse(
                registration_id="exiftool",
                contract_id=MEDIA_METADATA_OBSERVER_CONTRACT.id,
                artifact_rules=media_rules,
            ),
        ),
        routes=(
            RecipeRoute(
                id="archive",
                operation_id=AUDIO_ARCHIVE_OPERATION.id,
                target_registration_id="opus",
                artifact_rules=media_rules,
                output_tags=("archive",),
            ),
        ),
    )
    observers = MediaObservers()
    planner = RecipePlanner(
        catalog=RecipeCatalog(
            operations=(AUDIO_ARCHIVE_OPERATION,),
            recipes=(recipe,),
        ),
        riverhog=cast(ApiClient, AssociatedMediaCatalogApi()),
        observers=cast(ObserverPort, observers),
        targets=cast(TargetPort, ArchiveTargets()),
    )
    root = CollectionRootRef(
        collection_id=11,
        manifest_sha256=_sha("1"),
        content_identity=_sha("2"),
    )
    work = planner.create_work(recipe.id, (root,))
    request = planner.observation_requests(work)[0]
    observed_facts = MediaMetadataFacts(
        artifacts=tuple(
            MediaArtifactFacts(
                artifact_id=subject.id,
                state="observed",
                facts=(
                    MediaMetadataFact(
                        name="capture-time",
                        value="2025:02:03 04:05:06-08:00",
                        evidence=MediaFactEvidence(
                            artifact_id=subject.id,
                            field="XMP-xmp:CreateDate",
                        ),
                    ),
                ),
            )
            for subject in request.subjects
        )
    )
    result = ObservationResultBuilder(observers.value, request).observed(
        observed_facts.model_dump(mode="json")
    )
    evidence = ObservationEvidence(request=request, result=result)

    decision = planner.workflow_plan(work, (evidence,))

    assert isinstance(decision, BranchSetDecision)
    selection = decision.selection_documents[
        decision.plan.branches[0].artifact_selection.selection_sha256
    ]
    assert {artifact.path for artifact in selection.artifacts} == {
        "camera/clip.mov",
        "camera/clip.xmp",
    }
    assert {artifact.path: artifact.role for artifact in selection.artifacts} == {
        "camera/clip.mov": SOURCE_ROLE,
        "camera/clip.xmp": XMP_SOURCE_ROLE,
    }
    assert decision.plan.retirement_policy == "retain"
    assert decision.plan.evidence_sha256s == (result.result_sha256,)
    assert all(
        branch.workflow_plan.observations == (evidence,) for branch in decision.plan.branches
    )
    preflight = planner.target_preflight_request(
        decision.plan.branches[0].workflow_plan,
        decision.selection_documents,
    )
    assert preflight.observations == (evidence,)
    changed_facts = observed_facts.model_copy(
        update={
            "artifacts": (
                observed_facts.artifacts[0].model_copy(
                    update={
                        "facts": (
                            observed_facts.artifacts[0]
                            .facts[0]
                            .model_copy(update={"value": "2025:02:03 04:05:07-08:00"}),
                        )
                    }
                ),
                *observed_facts.artifacts[1:],
            )
        }
    )
    changed_result = ObservationResultBuilder(observers.value, request).observed(
        changed_facts.model_dump(mode="json")
    )
    changed = planner.workflow_plan(
        work,
        (ObservationEvidence(request=request, result=changed_result),),
    )
    assert isinstance(changed, BranchSetDecision)
    assert changed.plan.branch_set_sha256 != decision.plan.branch_set_sha256


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
        content_identity=_sha("2"),
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
                REVIEW_RCLONE_DELIVER_OPERATION,
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
            inputs=(
                InputArtifactContract(
                    role="fixture.source/v1",
                    allowed_dispositions=("transformed",),
                ),
            ),
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
            inputs=(
                InputArtifactContract(
                    role="fixture.branch-output/v1",
                    minimum=2,
                    allowed_dispositions=("transformed",),
                ),
            ),
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
        content_identity=_sha("2"),
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
            content_identity=f"{(collection_id + 1) % 16:x}" * 64,
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


def _retirement_operation() -> OperationContract:
    return OperationContract.seal(
        OperationContractPayload(
            id="fixture.retirement-copy/v1",
            intent_schema=JsonSchemaDocument.from_schema(
                "fixture.retirement-copy-options/v1",
                {"type": "object", "additionalProperties": False},
            ),
            inputs=(
                InputArtifactContract(
                    role="fixture.source/v1",
                    allowed_dispositions=("transformed",),
                ),
            ),
            outputs=(
                OutputArtifactContract(
                    role="fixture.output/v1",
                    derived_from_roles=("fixture.source/v1",),
                ),
            ),
            source_retirement_permitted=True,
        )
    )


class MultiArtifactCatalogApi(CatalogApi):
    def search(self, **_kwargs: object) -> dict[str, object]:
        return {
            "files": [
                {"path": "camera/source.mp4", "bytes": 100, "sha256": _sha("3")},
                {"path": "camera/source.json", "bytes": 20, "sha256": _sha("4")},
            ]
        }


def _retirement_planner(recipe: RecipeDefinition) -> RecipePlanner:
    operation = _retirement_operation()
    target = TargetContract.seal(
        TargetContractPayload(
            implementation_id="fixture.retirement-target/v1",
            implementation_version="1.0.0",
            source_revision="fixture",
            image_digest=_sha("9"),
            operations=(
                TargetOperationSupport(
                    operation_id=operation.id,
                    operation_contract_sha256=operation.contract_sha256,
                    options_schema=JsonSchemaDocument.from_schema(
                        "fixture.retirement-target-options/v1",
                        {"type": "object", "additionalProperties": False},
                    ),
                ),
            ),
        )
    )
    return RecipePlanner(
        catalog=RecipeCatalog(operations=(operation,), recipes=(recipe,)),
        riverhog=cast(ApiClient, MultiArtifactCatalogApi()),
        observers=cast(ObserverPort, object()),
        targets=cast(TargetPort, Targets(target)),
    )


def test_retirement_plan_accepts_overlapping_selections_covering_complete_inventory() -> None:
    recipe = RecipeDefinition(
        id="fixture.retirement/v1",
        revision=1,
        input_tags=("fixture",),
        source_retirement_policy="retire-after-verified-output",
        routes=(
            RecipeRoute(
                id="all",
                operation_id="fixture.retirement-copy/v1",
                target_registration_id="review-ffmpeg",
                artifact_rules=(ArtifactRule(role="fixture.source/v1"),),
                output_tags=("all",),
            ),
            RecipeRoute(
                id="video",
                operation_id="fixture.retirement-copy/v1",
                target_registration_id="review-ffmpeg",
                artifact_rules=(ArtifactRule(glob="*.mp4", role="fixture.source/v1"),),
                output_tags=("video",),
            ),
        ),
    )
    planner = _retirement_planner(recipe)
    root = CollectionRootRef(
        collection_id=11,
        manifest_sha256=_sha("1"),
        content_identity=_sha("2"),
    )

    decision = planner.workflow_plan(planner.create_work(recipe.id, (root,)), ())

    assert isinstance(decision, BranchSetDecision)
    assert decision.plan.retirement_policy == "retire-after-verified-output"
    selections = {
        branch.branch_id: decision.selection_documents[branch.artifact_selection.selection_sha256]
        for branch in decision.plan.branches
    }
    assert len(selections["all"].artifacts) == 2
    assert selections["video"].artifacts[0] in selections["all"].artifacts


def test_retirement_plan_rejects_incomplete_inventory_before_target_preflight() -> None:
    recipe = RecipeDefinition(
        id="fixture.retirement/v1",
        revision=1,
        input_tags=("fixture",),
        source_retirement_policy="retire-after-verified-output",
        routes=(
            RecipeRoute(
                id="video-only",
                operation_id="fixture.retirement-copy/v1",
                target_registration_id="review-ffmpeg",
                artifact_rules=(ArtifactRule(glob="*.mp4", role="fixture.source/v1"),),
                output_tags=("video",),
            ),
        ),
    )
    planner = _retirement_planner(recipe)
    root = CollectionRootRef(
        collection_id=11,
        manifest_sha256=_sha("1"),
        content_identity=_sha("2"),
    )

    decision = planner.workflow_plan(planner.create_work(recipe.id, (root,)), ())

    assert isinstance(decision, WorkInapplicable)
    assert decision.code == "unsafe-retirement-coverage"
    assert "camera/source.json" in decision.message


def test_catalog_rejects_retirement_recipe_using_audio_only_operation() -> None:
    recipe = RecipeDefinition(
        id="fixture.unsafe-audio-retirement/v1",
        revision=1,
        input_tags=("fixture",),
        source_retirement_policy="retire-after-verified-output",
        routes=(
            RecipeRoute(
                id="audio",
                operation_id=AUDIO_ARCHIVE_OPERATION.id,
                target_registration_id="opus",
                artifact_rules=(ArtifactRule(role="stove0.media.source/v1"),),
                output_tags=("audio",),
            ),
        ),
    )

    with pytest.raises(ValueError, match="do not authorize retirement: audio"):
        RecipeCatalog(operations=(AUDIO_ARCHIVE_OPERATION,), recipes=(recipe,))

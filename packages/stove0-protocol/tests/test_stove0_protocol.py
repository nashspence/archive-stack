from __future__ import annotations

import pytest
from pydantic import ValidationError
from stove0_protocol import (
    ArtifactSelection,
    ArtifactSubject,
    BranchPlan,
    BranchSetPlan,
    BranchTargetPreview,
    CollectionRootRef,
    ControllerEvidence,
    ControllerEvidencePayload,
    EvaluationDefinition,
    EvaluationDefinitionPayload,
    EvaluationMatrix,
    EvaluationMatrixPayload,
    EvaluationVariant,
    ExecutionEnvelope,
    ExecutionEnvelopePayload,
    JsonSchemaDocument,
    OperationRef,
    RecipeRef,
    TargetPlanBinding,
    WorkflowPlan,
    WorkflowPlanIntent,
    WorkflowPlanPayload,
    WorkflowPreview,
    WorkflowPreviewPayload,
    WorkflowPreviewRequest,
    WorkflowPreviewRequestPayload,
    WorkIdentity,
    WorkPayload,
    canonical_json_sha256,
)
from stove0_protocol.models import (
    ObservationEvidence,
    ObservationFailure,
    ObservationInapplicable,
    ObservationInvocation,
    ObservationRequest,
    ObservationRequestPayload,
    ObservationResult,
    ObservationResultPayload,
    ObserverContract,
    ObserverContractPayload,
    ObserverContractSupport,
    ObserverDescriptor,
    ObserverDescriptorPayload,
    ObserverImplementation,
    ObserverRuntimeAuthority,
    validate_observation_result,
)


def _sha(character: str) -> str:
    return character * 64


def _root(collection_id: int = 1) -> CollectionRootRef:
    return CollectionRootRef(
        collection_id=collection_id,
        manifest_sha256=_sha(str(collection_id)),
        content_identity=_sha(chr(ord("a") + collection_id)),
    )


def _work() -> WorkIdentity:
    return WorkIdentity.seal(
        WorkPayload(
            recipe=RecipeRef(id="camera.archive/v1", revision=3, sha256=_sha("a")),
            inputs=(_root(1), _root(2)),
            effective_intent={"preserve_original": True},
        )
    )


def _contract() -> ObserverContract:
    options = JsonSchemaDocument.from_schema(
        "camera.probe-options/v1",
        {"type": "object", "additionalProperties": False},
    )
    facts = JsonSchemaDocument.from_schema(
        "camera.probe-facts/v1",
        {
            "type": "object",
            "properties": {"streams": {"type": "integer"}},
            "required": ["streams"],
            "additionalProperties": False,
        },
    )
    return ObserverContract.seal(
        ObserverContractPayload(
            id="camera.probe/v1",
            options_schema=options,
            facts_schema=facts,
            maximum_result_bytes=8192,
        )
    )


def _descriptor(contract: ObserverContract) -> ObserverDescriptor:
    return ObserverDescriptor.seal(
        ObserverDescriptorPayload(
            implementation_id="fixture.camera-probe/v1",
            implementation_version="1.2.3",
            source_revision="fixture-revision",
            image_digest=_sha("9"),
            contracts=(ObserverContractSupport.from_contract(contract),),
        )
    )


def _subject() -> ArtifactSubject:
    return ArtifactSubject(
        id="camera-source",
        role="camera.source/v1",
        collection=_root(1),
        path="camera/source.mov",
        bytes=123,
        sha256=_sha("d"),
        media_type="video/quicktime",
    )


def _request(
    work: WorkIdentity,
    contract: ObserverContract,
    descriptor: ObserverDescriptor,
) -> ObservationRequest:
    return ObservationRequest.seal(
        ObservationRequestPayload(
            work_id=work.work_id,
            observer_registration_id="fixture-observer",
            observer_descriptor_sha256=descriptor.descriptor_sha256,
            observer_contract_id=contract.id,
            observer_contract_sha256=contract.contract_sha256,
            subjects=(_subject(),),
            options={},
            timeout_seconds=30,
            maximum_result_bytes=8192,
        )
    )


def _result(
    request: ObservationRequest,
    contract: ObserverContract,
    descriptor: ObserverDescriptor,
) -> ObservationResult:
    facts = {"streams": 2}
    return ObservationResult.seal(
        ObservationResultPayload(
            request_id=request.request_id,
            state="observed",
            observer=ObserverImplementation(
                id=descriptor.implementation_id,
                version=descriptor.implementation_version,
                source_revision=descriptor.source_revision,
                descriptor_sha256=descriptor.descriptor_sha256,
            ),
            observer_contract_id=contract.id,
            observer_contract_sha256=contract.contract_sha256,
            subjects=request.subjects,
            facts_schema=contract.facts_schema,
            facts=facts,
            facts_sha256=canonical_json_sha256(facts),
            execution_evidence={"tool": "fixture"},
        )
    )


def test_work_identity_is_deterministic_and_binds_ordered_roots() -> None:
    first = _work()
    second = _work()
    assert first == second
    assert first.work_id == second.work_id

    with pytest.raises(ValidationError, match="canonically ordered"):
        WorkPayload(
            recipe=first.recipe,
            inputs=tuple(reversed(first.inputs)),
            effective_intent=first.effective_intent,
        )


def test_observer_contract_descriptor_and_result_are_self_verifying() -> None:
    work = _work()
    contract = _contract()
    descriptor = _descriptor(contract)
    request = _request(work, contract, descriptor)
    result = _result(request, contract, descriptor)

    validate_observation_result(result, request, descriptor)
    assert result.result_sha256 == canonical_json_sha256(
        result.model_dump(
            mode="json",
            by_alias=True,
            exclude={"result_sha256"},
            exclude_none=True,
        )
    )

    invalid = result.model_dump(mode="json")
    invalid["facts"]["streams"] = 3
    with pytest.raises(ValidationError, match="facts digest"):
        ObservationResult.model_validate(invalid)


def test_non_observed_results_cannot_smuggle_facts() -> None:
    work = _work()
    contract = _contract()
    descriptor = _descriptor(contract)
    request = _request(work, contract, descriptor)

    with pytest.raises(ValidationError, match="cannot include facts"):
        ObservationResultPayload(
            request_id=request.request_id,
            state="inapplicable",
            observer=ObserverImplementation(
                id=descriptor.implementation_id,
                version=descriptor.implementation_version,
                source_revision=descriptor.source_revision,
                descriptor_sha256=descriptor.descriptor_sha256,
            ),
            observer_contract_id=contract.id,
            observer_contract_sha256=contract.contract_sha256,
            subjects=request.subjects,
            facts_schema=contract.facts_schema,
            facts={"streams": 0},
            facts_sha256=canonical_json_sha256({"streams": 0}),
        )


@pytest.mark.parametrize(
    ("state", "outcome"),
    [
        (
            "inapplicable",
            {"inapplicable": ObservationInapplicable(code="unsupported", message="No match")},
        ),
        (
            "failed",
            {
                "failure": ObservationFailure(
                    code="probe-failed", message="Probe failed", retryable=True
                )
            },
        ),
        ("canceled", {}),
    ],
)
def test_only_observed_results_are_planning_evidence(
    state: str,
    outcome: dict[str, object],
) -> None:
    work = _work()
    contract = _contract()
    descriptor = _descriptor(contract)
    request = _request(work, contract, descriptor)
    result = ObservationResult.seal(
        ObservationResultPayload(
            request_id=request.request_id,
            state=state,  # type: ignore[arg-type]
            observer=ObserverImplementation(
                id=descriptor.implementation_id,
                version=descriptor.implementation_version,
                source_revision=descriptor.source_revision,
                descriptor_sha256=descriptor.descriptor_sha256,
            ),
            observer_contract_id=contract.id,
            observer_contract_sha256=contract.contract_sha256,
            subjects=request.subjects,
            **outcome,
        )
    )

    with pytest.raises(ValidationError, match="only observed results"):
        ObservationEvidence(request=request, result=result)


def test_workflow_target_and_controller_evidence_bind_one_another() -> None:
    work = _work()
    contract = _contract()
    descriptor = _descriptor(contract)
    request = _request(work, contract, descriptor)
    observation = _result(request, contract, descriptor)
    operation = OperationRef(id="video.archive/v1", sha256=_sha("e"))
    workflow = WorkflowPlan.seal(
        WorkflowPlanPayload(
            work=work,
            observations=(ObservationEvidence(request=request, result=observation),),
            operation=operation,
            target_registration_id="nvenc-primary",
            target_contract_sha256=_sha("f"),
            requested_target_options={"preset": "p7"},
            output_tags=("archive-video",),
            retirement_policy="retain",
        )
    )
    target_document = {
        "format": "stove0-target-plan-fixture/v1",
        "operation_id": operation.id,
        "input_count": 2,
    }
    target = TargetPlanBinding(
        protocol="stove0-transform-target/v1",
        target_implementation_id="fixture.target/v1",
        target_contract_sha256=workflow.target_contract_sha256,
        operation_contract_sha256=operation.sha256,
        plan=target_document,
        plan_sha256=canonical_json_sha256(target_document),
    )
    envelope = ExecutionEnvelope.seal(
        ExecutionEnvelopePayload(
            claim_id="claim-1",
            fence=2,
            workflow_plan=workflow,
            target_plan=target,
        )
    )
    evidence = ControllerEvidence.seal(ControllerEvidencePayload(execution_envelope=envelope))

    assert evidence.execution_envelope.workflow_plan.observations[0].result.facts == {"streams": 2}
    assert evidence.controller_evidence_sha256 == canonical_json_sha256(
        evidence.model_dump(
            mode="json",
            by_alias=True,
            exclude={"controller_evidence_sha256"},
            exclude_none=True,
        )
    )

    with pytest.raises(ValidationError, match="target plan differs"):
        ExecutionEnvelopePayload(
            claim_id="claim-1",
            fence=2,
            workflow_plan=workflow,
            target_plan=target.model_copy(update={"target_contract_sha256": _sha("0")}),
        )


def test_observation_request_identity_is_independent_of_claim_generation() -> None:
    work = _work()
    contract = _contract()
    descriptor = _descriptor(contract)
    request = _request(work, contract, descriptor)

    first = ObservationInvocation(
        request=request,
        claim_id="preview-claim",
        fence=1,
        runtime=ObserverRuntimeAuthority(
            riverhog_base_url="https://riverhog.invalid",
            capability_token="first-secret",
            workspace_assurance="ephemeral",
        ),
    )
    second = ObservationInvocation(
        request=request,
        claim_id="transform-claim",
        fence=9,
        runtime=ObserverRuntimeAuthority(
            riverhog_base_url="https://riverhog.invalid",
            capability_token="second-secret",
            workspace_assurance="ephemeral",
        ),
    )
    assert first.request.request_id == second.request.request_id
    assert first.claim_id != second.claim_id
    assert first.fence != second.fence


def test_workflow_preview_and_evaluation_contracts_are_deterministic() -> None:
    work = _work()
    preview_request = WorkflowPreviewRequest.seal(WorkflowPreviewRequestPayload(work=work))
    operation = OperationRef(id="video.archive/v1", sha256=_sha("e"))
    selection = ArtifactSelection.seal(
        (
            ArtifactSubject(
                id="source",
                role="video.source/v1",
                collection=work.inputs[0],
                path="source.mp4",
                bytes=12,
                sha256=_sha("d"),
            ),
        )
    )
    branch = BranchPlan.build(
        parent_work=work,
        branch_id="archive",
        decision_sha256=_sha("c"),
        selection=selection,
        recipe=work.recipe,
        effective_intent=work.effective_intent,
        workflow_intent=WorkflowPlanIntent(
            operation=operation,
            target_registration_id="fixture-target",
            target_contract_sha256=_sha("f"),
            output_tags=("archive-video",),
            retirement_policy="retain",
        ),
    )
    workflow = branch.workflow_plan
    branch_set = BranchSetPlan.seal(
        parent_work=work,
        decision_sha256=_sha("c"),
        branches=(branch,),
        selections={selection.selection_sha256: selection},
    )
    target = TargetPlanBinding(
        protocol="stove0-transform-target/v1",
        target_implementation_id="fixture.target/v1",
        target_contract_sha256=workflow.target_contract_sha256,
        operation_contract_sha256=operation.sha256,
        plan={"format": "fixture-target-plan/v1"},
        plan_sha256=_sha("1"),
    )
    first = WorkflowPreview.seal(
        WorkflowPreviewPayload(
            preview_id=preview_request.preview_id,
            state="ready",
            work=work,
            branch_set_plan=branch_set,
            selections=(selection,),
            target_plans=(
                BranchTargetPreview(
                    branch_id="archive",
                    work_id=workflow.work.work_id,
                    workflow_plan_sha256=workflow.workflow_plan_sha256,
                    target_plan=target,
                ),
            ),
        )
    )
    second = WorkflowPreview.seal(
        WorkflowPreviewPayload(
            preview_id=preview_request.preview_id,
            state="ready",
            work=work,
            branch_set_plan=branch_set,
            selections=(selection,),
            target_plans=(
                BranchTargetPreview(
                    branch_id="archive",
                    work_id=workflow.work.work_id,
                    workflow_plan_sha256=workflow.workflow_plan_sha256,
                    target_plan=target,
                ),
            ),
        )
    )
    assert first == second

    matrix = EvaluationMatrix.seal(
        EvaluationMatrixPayload(
            variants=(
                EvaluationVariant(id="quality-24", parameters={"quality": 24}),
                EvaluationVariant(id="quality-30", parameters={"quality": 30}),
            )
        )
    )
    evaluation = EvaluationDefinition.seal(
        EvaluationDefinitionPayload(
            recipe=work.recipe,
            inputs=work.inputs,
            common_intent={"sample_plan_sha256": _sha("2")},
            matrix=matrix,
        )
    )
    children = evaluation.child_works()
    assert len(children) == 2
    assert children[0].work_id != children[1].work_id
    assert children[0].evaluation is not None
    assert children[0].evaluation.matrix_sha256 == matrix.matrix_sha256

    with pytest.raises(ValidationError, match="must retain"):
        WorkflowPlanPayload(
            work=children[0],
            operation=operation,
            target_registration_id="fixture-target",
            target_contract_sha256=_sha("f"),
            output_tags=("review-output",),
            retirement_policy="retire-after-verified-output",
        )


def test_observer_batch_preference_and_large_evaluation_are_supported() -> None:
    contract = ObserverContract.seal(
        ObserverContractPayload(
            id="fixture.large-observer/v1",
            options_schema=JsonSchemaDocument.from_schema(
                "fixture.large-observer-options/v1",
                {"type": "object", "additionalProperties": False},
            ),
            facts_schema=JsonSchemaDocument.from_schema(
                "fixture.large-observer-facts/v1",
                {"type": "object", "additionalProperties": True},
            ),
            maximum_result_bytes=1024,
        )
    )
    matrix = EvaluationMatrix.seal(
        EvaluationMatrixPayload(
            variants=tuple(EvaluationVariant(id=f"variant-{index:04}") for index in range(257))
        )
    )

    support = ObserverContractSupport.from_contract(
        contract,
        preferred_subject_batch_size=10_001,
    )
    assert support.preferred_subject_batch_size == 10_001
    assert len(matrix.variants) == 257

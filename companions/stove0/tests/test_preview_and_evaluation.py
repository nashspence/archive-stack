from __future__ import annotations

from pathlib import Path

from stove0_core import (
    ClaimBinding,
    EvaluationService,
    InMemoryEvaluationStore,
    InMemoryWorkStore,
    SqlAlchemyStateStore,
    Stove0WorkService,
    WorkFailure,
    WorkflowPreviewService,
    WorkRecord,
)
from stove0_protocol import (
    ArtifactSelection,
    ArtifactSubject,
    BranchPlan,
    BranchSetDecision,
    BranchSetPlan,
    CollectionRootRef,
    EvaluationDefinition,
    EvaluationDefinitionPayload,
    EvaluationMatrix,
    EvaluationMatrixPayload,
    EvaluationVariant,
    JsonSchemaDocument,
    ObservationEvidence,
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
    OperationRef,
    RecipeRef,
    WorkflowPlan,
    WorkflowPlanIntent,
    WorkIdentity,
    WorkPayload,
    canonical_json_sha256,
)
from stove0_target_support import (
    InputArtifact,
    InputArtifactContract,
    OperationContract,
    OperationContractPayload,
    OutputArtifactContract,
    OutputCollectionRef,
    TargetContract,
    TargetContractPayload,
    TargetOperationSupport,
    TargetPreflightRequest,
    TargetPreflightResponse,
    TransformPlan,
    TransformPlanPayload,
)


def _sha(character: str) -> str:
    return character * 64


def _root() -> CollectionRootRef:
    return CollectionRootRef(
        collection_id=1,
        manifest_sha256=_sha("1"),
        content_etag=_sha("2"),
    )


def _work() -> WorkIdentity:
    return WorkIdentity.seal(
        WorkPayload(
            recipe=RecipeRef(id="fixture.recipe/v1", revision=1, sha256=_sha("3")),
            inputs=(_root(),),
            effective_intent={"suffix": ".copy"},
        )
    )


def _operation() -> OperationContract:
    return OperationContract.seal(
        OperationContractPayload(
            id="fixture.copy/v1",
            intent_schema=JsonSchemaDocument.from_schema(
                "fixture.copy-intent/v1",
                {
                    "type": "object",
                    "properties": {"suffix": {"type": "string"}},
                    "required": ["suffix"],
                    "additionalProperties": False,
                },
            ),
            inputs=(InputArtifactContract(role="fixture.source/v1"),),
            outputs=(
                OutputArtifactContract(
                    role="fixture.output/v1",
                    derived_from_roles=("fixture.source/v1",),
                ),
            ),
        )
    )


def _target(operation: OperationContract) -> TargetContract:
    return TargetContract.seal(
        TargetContractPayload(
            implementation_id="fixture.target/v1",
            implementation_version="1.0.0",
            source_revision="fixture",
            image_digest=_sha("9"),
            operations=(
                TargetOperationSupport(
                    operation_id=operation.id,
                    operation_contract_sha256=operation.contract_sha256,
                    options_schema=JsonSchemaDocument.from_schema(
                        "fixture.target-options/v1",
                        {"type": "object", "additionalProperties": False},
                    ),
                ),
            ),
        )
    )


def _observer() -> tuple[ObserverContract, ObserverDescriptor]:
    contract = ObserverContract.seal(
        ObserverContractPayload(
            id="fixture.kind/v1",
            options_schema=JsonSchemaDocument.from_schema(
                "fixture.kind-options/v1",
                {"type": "object", "additionalProperties": False},
            ),
            facts_schema=JsonSchemaDocument.from_schema(
                "fixture.kind-facts/v1",
                {
                    "type": "object",
                    "properties": {"kind": {"type": "string"}},
                    "required": ["kind"],
                    "additionalProperties": False,
                },
            ),
        )
    )
    descriptor = ObserverDescriptor.seal(
        ObserverDescriptorPayload(
            implementation_id="fixture.observer/v1",
            implementation_version="1.0.0",
            source_revision="fixture",
            image_digest=_sha("9"),
            contracts=(ObserverContractSupport.from_contract(contract),),
        )
    )
    return contract, descriptor


class PreviewPlanning:
    def __init__(
        self,
        operation: OperationContract,
        target: TargetContract,
        observer: tuple[ObserverContract, ObserverDescriptor],
    ) -> None:
        self.operation = operation
        self.target = target
        self.observer = observer

    def observation_requests(self, work: WorkIdentity) -> tuple[ObservationRequest, ...]:
        contract, descriptor = self.observer
        return (
            ObservationRequest.seal(
                ObservationRequestPayload(
                    work_id=work.work_id,
                    observer_registration_id="fixture-observer",
                    observer_descriptor_sha256=descriptor.descriptor_sha256,
                    observer_contract_id=contract.id,
                    observer_contract_sha256=contract.contract_sha256,
                    subjects=(
                        ArtifactSubject(
                            id="source",
                            role="fixture.source/v1",
                            collection=_root(),
                            path="source/input.bin",
                            bytes=12,
                            sha256=_sha("4"),
                        ),
                    ),
                )
            ),
        )

    def workflow_plan(
        self,
        work: WorkIdentity,
        observations: tuple[ObservationEvidence, ...],
    ) -> BranchSetDecision:
        selection = ArtifactSelection.seal(observations[0].request.subjects)
        branch = BranchPlan.build(
            parent_work=work,
            branch_id="fixture",
            decision_sha256=_sha("d"),
            selection=selection,
            recipe=work.recipe,
            effective_intent={"suffix": ".copy"},
            workflow_intent=WorkflowPlanIntent(
                operation=OperationRef(
                    id=self.operation.id,
                    sha256=self.operation.contract_sha256,
                ),
                target_registration_id="fixture-target",
                target_contract_sha256=self.target.contract_sha256,
                output_tags=("fixture-output",),
                retirement_policy="retain",
            ),
            observations=observations,
        )
        return BranchSetDecision(
            plan=BranchSetPlan.seal(
                parent_work=work,
                decision_sha256=_sha("d"),
                evidence_sha256s=(observations[0].result.result_sha256,),
                branches=(branch,),
                selections={selection.selection_sha256: selection},
            ),
            selections=(selection,),
        )

    def target_preflight_request(
        self,
        _plan: WorkflowPlan,
        _selections: dict[str, ArtifactSelection],
    ) -> TargetPreflightRequest:
        return TargetPreflightRequest(
            operation_id=self.operation.id,
            operation_contract_sha256=self.operation.contract_sha256,
            inputs=(
                InputArtifact(
                    id="source",
                    role="fixture.source/v1",
                    collection=_root(),
                    path="source/input.bin",
                    bytes=12,
                    sha256=_sha("4"),
                ),
            ),
            intent={"suffix": ".copy"},
            target_options={},
        )

    def operation_contract(self, operation: OperationRef) -> OperationContract:
        assert operation.sha256 == self.operation.contract_sha256
        return self.operation


class PreviewRiverhog:
    def __init__(self) -> None:
        self.next_fence = 0
        self.abandoned: list[tuple[str, int]] = []
        self.actions: list[str] = []

    def acquire_preview_claim(self, request: object) -> ClaimBinding:
        self.next_fence += 1
        self.actions.append("acquire-read-only-preview")
        return ClaimBinding(claim_id=f"preview-{self.next_fence}", fence=self.next_fence)

    def observation_authority(
        self,
        claim: ClaimBinding,
        request: ObservationRequest,
    ) -> ObserverRuntimeAuthority:
        assert request.work_id == _work().work_id
        self.actions.append("read-inputs")
        return ObserverRuntimeAuthority(
            riverhog_base_url="https://riverhog.invalid",
            capability_token=f"read-only-{claim.fence}",
            workspace_assurance="ephemeral",
        )

    def abandon_preview_claim(self, request: object, claim: ClaimBinding) -> None:
        self.actions.append("abandon-preview")
        self.abandoned.append((claim.claim_id, claim.fence))


class PreviewObserver:
    def __init__(self, value: tuple[ObserverContract, ObserverDescriptor]) -> None:
        self.contract, self.value = value
        self.invocations: list[ObservationInvocation] = []

    def descriptor(self, registration_id: str) -> ObserverDescriptor:
        assert registration_id == "fixture-observer"
        return self.value

    def observe(
        self,
        registration_id: str,
        invocation: ObservationInvocation,
    ) -> ObservationResult:
        assert registration_id == "fixture-observer"
        self.invocations.append(invocation)
        facts = {"kind": "fixture"}
        return ObservationResult.seal(
            ObservationResultPayload(
                request_id=invocation.request.request_id,
                state="observed",
                observer=ObserverImplementation(
                    id=self.value.implementation_id,
                    version=self.value.implementation_version,
                    source_revision=self.value.source_revision,
                    descriptor_sha256=self.value.descriptor_sha256,
                ),
                observer_contract_id=self.contract.id,
                observer_contract_sha256=self.contract.contract_sha256,
                subjects=invocation.request.subjects,
                facts_schema=self.contract.facts_schema,
                facts=facts,
                facts_sha256=canonical_json_sha256(facts),
            )
        )


class PreviewTarget:
    def __init__(self, operation: OperationContract, target: TargetContract) -> None:
        self.operation = operation
        self.target = target
        self.preflights = 0

    def contract(self, registration_id: str) -> TargetContract:
        assert registration_id == "fixture-target"
        return self.target

    def preflight(
        self,
        registration_id: str,
        request: TargetPreflightRequest,
    ) -> TargetPreflightResponse:
        assert registration_id == "fixture-target"
        self.preflights += 1
        return TargetPreflightResponse(
            target=self.target,
            plan=TransformPlan.seal(
                TransformPlanPayload(
                    target_implementation_id=self.target.implementation_id,
                    target_contract_sha256=self.target.contract_sha256,
                    operation_id=request.operation_id,
                    operation_contract_sha256=request.operation_contract_sha256,
                    inputs=request.inputs,
                    intent=request.intent,
                    target_options=request.target_options,
                )
            ),
        )

    def put_job(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("workflow preview must not start target execution")

    def get_job(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("workflow preview must not poll target execution")

    def cancel_job(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("workflow preview must not create target jobs")


def test_workflow_preview_is_deterministic_across_claim_generations() -> None:
    operation = _operation()
    target = _target(operation)
    observer_value = _observer()
    riverhog = PreviewRiverhog()
    observer = PreviewObserver(observer_value)
    target_port = PreviewTarget(operation, target)
    service = WorkflowPreviewService(
        riverhog=riverhog,
        planning=PreviewPlanning(operation, target, observer_value),
        observers=observer,
        targets=target_port,
    )

    first = service.preview(_work())
    second = service.preview(_work())

    assert first.state == second.state == "ready"
    assert first.preview_id == second.preview_id
    assert first.preview_sha256 == second.preview_sha256
    assert first.branch_set_plan == second.branch_set_plan
    assert first.selections == second.selections
    assert first.target_plans == second.target_plans
    assert len(first.target_plans) == 1
    assert [item.fence for item in observer.invocations] == [1, 2]
    assert len(riverhog.abandoned) == 2
    assert "write-output" not in riverhog.actions
    assert target_port.preflights == 2


class FinishingController:
    def __init__(self, work: Stove0WorkService, *, failed_variant: str | None = None) -> None:
        self.work = work
        self.failed_variant = failed_variant

    def create_or_resume(self, identity: object) -> WorkRecord:
        return self.work.create_or_resume(WorkIdentity.model_validate(identity))

    def step(self, work_id: str) -> WorkRecord:
        record = self.work.store.load(work_id)
        assert record is not None
        variant = record.work.evaluation
        assert variant is not None
        if variant.variant_id == self.failed_variant:
            replacement = WorkRecord.model_validate(
                record.model_copy(
                    update={
                        "phase": "failed",
                        "revision": record.revision + 1,
                        "failure": WorkFailure(
                            code="fixture-failure",
                            message="variant failed",
                            retryable=True,
                        ),
                    }
                ).model_dump(mode="python")
            )
        else:
            output = OutputCollectionRef(
                collection_id=100 + len(variant.variant_id),
                manifest_sha256=_sha("a"),
                content_etag=_sha("b"),
                derivation_sha256=_sha("c"),
            )
            replacement = WorkRecord.model_validate(
                record.model_copy(
                    update={
                        "phase": "complete",
                        "revision": record.revision + 1,
                        "claim": ClaimBinding(claim_id=record.work_id, fence=1),
                        "output": output,
                    }
                ).model_dump(mode="python")
            )
        return self.work.store.compare_and_swap(
            work_id,
            expected_revision=record.revision,
            replacement=replacement,
        )

    def cancel(self, work_id: str, *, reason: str | None = None) -> WorkRecord:
        record = self.work.store.load(work_id)
        assert record is not None
        replacement = WorkRecord.model_validate(
            record.model_copy(
                update={"phase": "canceled", "revision": record.revision + 1}
            ).model_dump(mode="python")
        )
        return self.work.store.compare_and_swap(
            work_id,
            expected_revision=record.revision,
            replacement=replacement,
        )

    def retry(self, work_id: str) -> WorkRecord:
        record = self.work.store.load(work_id)
        assert record is not None
        self.failed_variant = None
        replacement = WorkRecord.model_validate(
            record.model_copy(
                update={
                    "phase": "eligible",
                    "revision": record.revision + 1,
                    "failure": None,
                }
            ).model_dump(mode="python")
        )
        return self.work.store.compare_and_swap(
            work_id,
            expected_revision=record.revision,
            replacement=replacement,
        )


def _evaluation() -> EvaluationDefinition:
    return EvaluationDefinition.seal(
        EvaluationDefinitionPayload(
            recipe=RecipeRef(id="review.recipe/v1", revision=1, sha256=_sha("5")),
            inputs=(_root(),),
            common_intent={"sample_plan_sha256": _sha("6")},
            matrix=EvaluationMatrix.seal(
                EvaluationMatrixPayload(
                    variants=(
                        EvaluationVariant(id="quality-24", parameters={"quality": 24}),
                        EvaluationVariant(id="quality-28", parameters={"quality": 28}),
                        EvaluationVariant(id="quality-32", parameters={"quality": 32}),
                    )
                )
            ),
        )
    )


def test_evaluation_aggregates_ordinary_child_work_and_partial_success() -> None:
    work = Stove0WorkService(InMemoryWorkStore())
    service = EvaluationService(InMemoryEvaluationStore(), work=work)
    controller = FinishingController(work, failed_variant="quality-28")
    record = service.create_or_resume(_evaluation())
    assert len(record.children) == 3
    assert record.phase == "running"

    for _ in range(3):
        record = service.step(record.evaluation_id, controller=controller)
    assert record.phase == "partially_complete"
    assert [item.state for item in record.children] == ["complete", "failed", "complete"]
    assert len([item.output for item in record.children if item.output is not None]) == 2

    record = service.retry_failed(
        record.evaluation_id,
        "quality-28",
        controller=controller,
    )
    assert record.phase == "running"
    record = service.step(record.evaluation_id, controller=controller)
    assert record.phase == "complete"
    assert all(item.output is not None for item in record.children)


def test_unified_evaluation_store_is_restart_safe(tmp_path: Path) -> None:
    path = tmp_path / "state" / "stove0.sqlite"
    path.parent.mkdir(mode=0o700)
    work = Stove0WorkService(InMemoryWorkStore())
    first_state = SqlAlchemyStateStore(f"sqlite+pysqlite:///{path}")
    first = EvaluationService(first_state.evaluation_store(), work=work)
    created = first.create_or_resume(_evaluation())

    second_state = SqlAlchemyStateStore(f"sqlite+pysqlite:///{path}")
    second = EvaluationService(second_state.evaluation_store(), work=work)
    loaded = second.refresh(created.evaluation_id)
    assert loaded.definition == created.definition
    assert loaded.children == created.children


def test_workflow_preview_rejects_observer_result_that_does_not_bind_request() -> None:
    operation = _operation()
    target = _target(operation)
    observer_value = _observer()
    riverhog = PreviewRiverhog()

    class InvalidObserver(PreviewObserver):
        def observe(
            self,
            registration_id: str,
            invocation: ObservationInvocation,
        ) -> ObservationResult:
            result = super().observe(registration_id, invocation)
            wrong_subject = ArtifactSubject(
                id="other",
                role="fixture.source/v1",
                collection=_root(),
                path="source/other.bin",
                bytes=12,
                sha256=_sha("4"),
            )
            return ObservationResult.seal(
                ObservationResultPayload(
                    **result.model_dump(
                        mode="python",
                        exclude={"result_sha256", "subjects"},
                    ),
                    subjects=(wrong_subject,),
                )
            )

    service = WorkflowPreviewService(
        riverhog=riverhog,
        planning=PreviewPlanning(operation, target, observer_value),
        observers=InvalidObserver(observer_value),
        targets=PreviewTarget(operation, target),
    )

    preview = service.preview(_work())

    assert preview.state == "failed"
    assert preview.outcome is not None
    assert "subjects differ" in preview.outcome.message
    assert len(riverhog.abandoned) == 1


def test_canceling_evaluation_retains_completed_variant_outputs() -> None:
    work = Stove0WorkService(InMemoryWorkStore())
    service = EvaluationService(InMemoryEvaluationStore(), work=work)
    controller = FinishingController(work)
    record = service.create_or_resume(_evaluation())

    record = service.step(record.evaluation_id, controller=controller)
    assert record.children[0].state == "complete"
    completed_output = record.children[0].output

    record = service.cancel(
        record.evaluation_id,
        controller=controller,
        reason="operator stopped evaluation",
    )

    assert record.phase == "partially_complete"
    assert record.children[0].output == completed_output
    assert [item.state for item in record.children[1:]] == ["canceled", "canceled"]

from __future__ import annotations

from riverhog_protocol.collection_workflows import (
    ArtifactDispositionSetIdentity,
    CollectionDerivation,
)
from riverhog_protocol.collection_workflows import (
    canonical_json_sha256 as riverhog_canonical_json_sha256,
)
from stove0_core import (
    ClaimBinding,
    InMemoryWorkStore,
    ParentOutcomeBinding,
    Stove0Coordinator,
    Stove0WorkService,
    TargetInvocationAuthority,
    WorkFailure,
    WorkInapplicable,
    WorkRecord,
)
from stove0_observer_protocol import (
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
)
from stove0_protocol import (
    JSON_SCHEMA_ONLY_SEMANTIC_PROFILE,
    ArtifactSelection,
    ArtifactSubject,
    BranchPlan,
    BranchSetDecision,
    BranchSetEvaluation,
    BranchSetPlan,
    BranchTargetPreview,
    BranchWorkBinding,
    CollectionRootRef,
    CoordinationBranchPlan,
    JoinDeclaration,
    JoinMemberDeclaration,
    JoinWorkBinding,
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
from stove0_target_protocol import (
    InputDispositionDeclaration,
    OutputArtifactSetIdentity,
    OutputSourceEdge,
    TargetCallbackAccess,
    TargetInputAuthority,
    TargetOutputBindingSetIdentity,
    TargetProductionAuthority,
    TargetProductionAuthorityPayload,
    TargetSettlementAuthority,
    TargetSettlementAuthorityPayload,
)
from stove0_target_support import (
    AcceptedTargetJob,
    InputArtifact,
    InputArtifactContract,
    OperationContract,
    OperationContractPayload,
    OutputArtifact,
    OutputArtifactContract,
    OutputCollectionRef,
    TargetContract,
    TargetContractPayload,
    TargetExecutionEvidence,
    TargetJobRequest,
    TargetJobStatus,
    TargetOperationSupport,
    TargetPreflightRequest,
    TargetPreflightResponse,
    TargetProgress,
    TargetRuntimeAuthority,
    TransformPlan,
    TransformPlanPayload,
)


def _sha(character: str) -> str:
    return character * 64


def _root() -> CollectionRootRef:
    return CollectionRootRef(
        collection_id=1,
        archive_root_sha256=_sha("1"),
        content_identity=_sha("2"),
    )


def _work() -> WorkIdentity:
    return WorkIdentity.seal(
        WorkPayload(
            recipe=RecipeRef(id="fixture.recipe/v1", revision=1, sha256=_sha("3")),
            inputs=(_root(),),
            effective_intent={"suffix": ".copy"},
        )
    )


def _operation(*, source_retirement_permitted: bool = False) -> OperationContract:
    return OperationContract.seal(
        OperationContractPayload(
            id="fixture.copy/v1",
            intent_semantics=JSON_SCHEMA_ONLY_SEMANTIC_PROFILE,
            intent_schema=JsonSchemaDocument.from_schema(
                "fixture.copy-intent/v1",
                {
                    "type": "object",
                    "properties": {"suffix": {"type": "string"}},
                    "required": ["suffix"],
                    "additionalProperties": False,
                },
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
            source_retirement_permitted=source_retirement_permitted,
        )
    )


def _fork_join_operations() -> tuple[OperationContract, OperationContract]:
    intent = JsonSchemaDocument.from_schema(
        "fixture.empty-intent/v1",
        {"type": "object", "additionalProperties": False},
    )
    branch = OperationContract.seal(
        OperationContractPayload(
            id="fixture.branch/v1",
            intent_semantics=JSON_SCHEMA_ONLY_SEMANTIC_PROFILE,
            intent_schema=intent,
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
    join = OperationContract.seal(
        OperationContractPayload(
            id="fixture.join/v1",
            intent_semantics=JSON_SCHEMA_ONLY_SEMANTIC_PROFILE,
            intent_schema=intent,
            inputs=(
                InputArtifactContract(
                    role="fixture.branch-output/v1",
                    minimum=2,
                    allowed_dispositions=("transformed",),
                ),
            ),
            outputs=(
                OutputArtifactContract(
                    role="fixture.joined-output/v1",
                    derived_from_roles=("fixture.branch-output/v1",),
                ),
            ),
        )
    )
    return branch, join


def _fork_join_target(
    operations: tuple[OperationContract, OperationContract],
) -> TargetContract:
    return TargetContract.seal(
        TargetContractPayload(
            implementation_id="fixture.fork-join-target/v1",
            implementation_version="1.0.0",
            source_revision="fixture",
            image_digest=_sha("8"),
            operations=tuple(
                TargetOperationSupport(
                    operation_id=operation.id,
                    operation_contract_sha256=operation.contract_sha256,
                    options_schema=JsonSchemaDocument.from_schema(
                        f"{operation.id}.options",
                        {"type": "object", "additionalProperties": False},
                    ),
                )
                for operation in operations
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


def _input() -> InputArtifact:
    return InputArtifact(
        id="source",
        role="fixture.source/v1",
        collection=_root(),
        path="source/input.bin",
        bytes=12,
        sha256=_sha("4"),
    )


def _observer() -> tuple[ObserverContract, ObserverDescriptor]:
    contract = ObserverContract.seal(
        ObserverContractPayload(
            id="fixture.kind/v1",
            facts_semantics=JSON_SCHEMA_ONLY_SEMANTIC_PROFILE,
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


def _target_input_selection(
    plan: WorkflowPlan,
    selections: dict[str, ArtifactSelection],
) -> ArtifactSelection:
    binding = plan.work.fork_join
    if isinstance(binding, BranchWorkBinding):
        return selections[binding.artifact_selection_sha256]
    if not isinstance(binding, JoinWorkBinding):
        raise RuntimeError("fixture target work has no exact input binding")
    artifacts: list[ArtifactSubject] = []
    for member in binding.members:
        selection = selections[member.artifact_selection_sha256]
        for subject in selection.artifacts:
            artifacts.append(
                subject.model_copy(
                    update={
                        "id": "j-"
                        + canonical_json_sha256(
                            {
                                "branch_id": member.branch_id,
                                "selection_sha256": member.artifact_selection_sha256,
                                "artifact_id": subject.id,
                            }
                        )[:32]
                    }
                )
            )
    return ArtifactSelection.seal(tuple(sorted(artifacts, key=lambda item: item.id)))


class FixturePlanning:
    def __init__(
        self,
        operation: OperationContract,
        target: TargetContract,
        observer: tuple[ObserverContract, ObserverDescriptor] | None,
    ) -> None:
        self.operation = operation
        self.target = target
        self.observer = observer

    def observation_requests(
        self,
        work: WorkIdentity,
    ) -> tuple[ObservationRequest, ...]:
        if self.observer is None:
            return ()
        contract, descriptor = self.observer
        subject = ArtifactSubject(
            id="source",
            role="fixture.source/v1",
            collection=_root(),
            path="source/input.bin",
            bytes=12,
            sha256=_sha("4"),
        )
        return (
            ObservationRequest.seal(
                ObservationRequestPayload(
                    work_id=work.work_id,
                    observer_registration_id="fixture-observer",
                    observer_descriptor_sha256=descriptor.descriptor_sha256,
                    observer_contract_id=contract.id,
                    observer_contract_sha256=contract.contract_sha256,
                    subjects=(subject,),
                )
            ),
        )

    def workflow_plan(
        self,
        work: WorkIdentity,
        observations: tuple[ObservationEvidence, ...],
        *,
        nested_observer: object | None = None,
    ) -> BranchSetDecision:
        subjects = (
            observations[0].request.subjects
            if observations
            else (
                ArtifactSubject(
                    id="source",
                    role="fixture.source/v1",
                    collection=_root(),
                    path="source/input.bin",
                    bytes=12,
                    sha256=_sha("4"),
                ),
            )
        )
        selection = ArtifactSelection.seal(subjects)
        branch = BranchPlan.build(
            parent_work=work,
            branch_id="fixture",
            decision_sha256=_sha("d"),
            selection=selection,
            recipe=work.recipe,
            effective_intent=work.effective_intent,
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
                evidence_sha256s=tuple(item.result.result_sha256 for item in observations),
                branches=(branch,),
                selections={selection.selection_sha256: selection},
            ),
            selections=(selection,),
        )

    def target_preflight_request(
        self,
        plan: WorkflowPlan,
        selections: dict[str, ArtifactSelection],
    ) -> TargetPreflightRequest:
        selection = next(iter(selections.values()))
        return TargetPreflightRequest(
            operation_id=self.operation.id,
            operation_contract_sha256=self.operation.contract_sha256,
            inputs=TargetInputAuthority.from_selection(selection),
            intent={"suffix": ".copy"},
            target_options={},
            observations=plan.observations,
        )

    def target_input_selection(
        self,
        plan: WorkflowPlan,
        selections: dict[str, ArtifactSelection],
    ) -> ArtifactSelection:
        return _target_input_selection(plan, selections)

    def operation_contract(self, operation: OperationRef) -> OperationContract:
        assert operation.id == self.operation.id
        assert operation.sha256 == self.operation.contract_sha256
        return self.operation


class ForkJoinPlanning:
    def __init__(
        self,
        branch_operation: OperationContract,
        join_operation: OperationContract,
        target: TargetContract,
    ) -> None:
        self.operations = {
            branch_operation.id: branch_operation,
            join_operation.id: join_operation,
        }
        self.branch_operation = branch_operation
        self.join_operation = join_operation
        self.target = target

    def observation_requests(self, _work: WorkIdentity) -> tuple[ObservationRequest, ...]:
        return ()

    def workflow_plan(
        self,
        work: WorkIdentity,
        observations: tuple[ObservationEvidence, ...],
        *,
        nested_observer: object | None = None,
    ) -> BranchSetDecision:
        assert not observations
        selection = ArtifactSelection.seal(
            (
                ArtifactSubject(
                    id="source",
                    role="fixture.source/v1",
                    collection=_root(),
                    path="source/input.bin",
                    bytes=12,
                    sha256=_sha("4"),
                ),
            )
        )
        decision = _sha("e")
        branches = tuple(
            BranchPlan.build(
                parent_work=work,
                branch_id=branch_id,
                decision_sha256=decision,
                selection=selection,
                recipe=work.recipe,
                effective_intent={"branch": branch_id},
                workflow_intent=WorkflowPlanIntent(
                    operation=OperationRef(
                        id=self.branch_operation.id,
                        sha256=self.branch_operation.contract_sha256,
                    ),
                    target_registration_id="fixture-target",
                    target_contract_sha256=self.target.contract_sha256,
                    output_tags=(f"fixture-{branch_id}",),
                    retirement_policy="retain",
                ),
            )
            for branch_id in ("audio", "video")
        )
        join = JoinDeclaration.seal(
            members=tuple(
                JoinMemberDeclaration(
                    branch_id=branch.branch_id,
                    output_roles=("fixture.branch-output/v1",),
                )
                for branch in branches
            ),
            recipe=work.recipe,
            effective_intent={"combine": "exact"},
            workflow_intent=WorkflowPlanIntent(
                operation=OperationRef(
                    id=self.join_operation.id,
                    sha256=self.join_operation.contract_sha256,
                ),
                target_registration_id="fixture-target",
                target_contract_sha256=self.target.contract_sha256,
                output_tags=("fixture-joined",),
                retirement_policy="retain",
            ),
        )
        documents = {selection.selection_sha256: selection}
        return BranchSetDecision(
            plan=BranchSetPlan.seal(
                parent_work=work,
                decision_sha256=decision,
                branches=branches,
                join=join,
                selections=documents,
            ),
            selections=(selection,),
        )

    def target_preflight_request(
        self,
        plan: WorkflowPlan,
        selections: dict[str, ArtifactSelection],
    ) -> TargetPreflightRequest:
        selection = self.target_input_selection(plan, selections)
        return TargetPreflightRequest(
            operation_id=plan.operation.id,
            operation_contract_sha256=plan.operation.sha256,
            inputs=TargetInputAuthority.from_selection(selection),
            intent=plan.work.effective_intent,
        )

    def target_input_selection(
        self,
        plan: WorkflowPlan,
        selections: dict[str, ArtifactSelection],
    ) -> ArtifactSelection:
        return _target_input_selection(plan, selections)

    def operation_contract(self, operation: OperationRef) -> OperationContract:
        contract = self.operations[operation.id]
        assert operation.sha256 == contract.contract_sha256
        return contract


class NestedPlanning(FixturePlanning):
    def workflow_plan(
        self,
        work: WorkIdentity,
        observations: tuple[ObservationEvidence, ...],
        *,
        nested_observer: object | None = None,
    ) -> BranchSetDecision:
        assert not observations
        selection = ArtifactSelection.seal(
            (
                ArtifactSubject(
                    id="source",
                    role="fixture.source/v1",
                    collection=_root(),
                    path="source/input.bin",
                    bytes=12,
                    sha256=_sha("4"),
                ),
            )
        )
        child_work = CoordinationBranchPlan.build_work(
            parent_work=work,
            branch_id="nested",
            decision_sha256=_sha("d"),
            selection=selection,
            recipe=RecipeRef(id="fixture.child/v1", revision=1, sha256=_sha("5")),
            effective_intent={"suffix": ".copy"},
        )
        leaf = BranchPlan.build(
            parent_work=child_work,
            branch_id="leaf",
            decision_sha256=_sha("e"),
            selection=selection,
            recipe=child_work.recipe,
            effective_intent=child_work.effective_intent,
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
        )
        child_plan = BranchSetPlan.seal(
            parent_work=child_work,
            decision_sha256=_sha("e"),
            branches=(leaf,),
            selections={selection.selection_sha256: selection},
        )
        root_plan = BranchSetPlan.seal(
            parent_work=work,
            decision_sha256=_sha("d"),
            branches=(
                CoordinationBranchPlan(
                    branch_id="nested",
                    artifact_selection=selection.ref(),
                    work=child_work,
                    branch_set_sha256=child_plan.branch_set_sha256,
                ),
            ),
            selections={selection.selection_sha256: selection},
            branch_sets={child_plan.branch_set_sha256: child_plan},
        )
        return BranchSetDecision(
            plan=root_plan,
            selections=(selection,),
            branch_sets=(child_plan,),
        )


class NestedJoinPlanning(ForkJoinPlanning):
    def workflow_plan(
        self,
        work: WorkIdentity,
        observations: tuple[ObservationEvidence, ...],
        *,
        nested_observer: object | None = None,
    ) -> BranchSetDecision:
        assert not observations
        selection = ArtifactSelection.seal(
            (
                ArtifactSubject(
                    id="source",
                    role="fixture.source/v1",
                    collection=_root(),
                    path="source/input.bin",
                    bytes=12,
                    sha256=_sha("4"),
                ),
            )
        )
        child_work = CoordinationBranchPlan.build_work(
            parent_work=work,
            branch_id="nested",
            decision_sha256=_sha("d"),
            selection=selection,
            recipe=RecipeRef(id="fixture.child/v1", revision=1, sha256=_sha("5")),
            effective_intent={},
        )
        child_decision = super().workflow_plan(child_work, ())
        child_plan = child_decision.plan
        root_plan = BranchSetPlan.seal(
            parent_work=work,
            decision_sha256=_sha("d"),
            branches=(
                CoordinationBranchPlan(
                    branch_id="nested",
                    artifact_selection=selection.ref(),
                    work=child_work,
                    branch_set_sha256=child_plan.branch_set_sha256,
                ),
            ),
            selections={selection.selection_sha256: selection},
            branch_sets={child_plan.branch_set_sha256: child_plan},
        )
        return BranchSetDecision(
            plan=root_plan,
            selections=(selection,),
            branch_sets=(child_plan,),
        )


class InapplicablePlanning(FixturePlanning):
    def workflow_plan(
        self,
        work: WorkIdentity,
        observations: tuple[ObservationEvidence, ...],
        *,
        nested_observer: object | None = None,
    ) -> WorkInapplicable:
        assert work.work_id
        assert not observations
        return WorkInapplicable(
            code="not-applicable",
            message="fixture policy selected no transform",
        )


class FixtureObservers:
    def __init__(self, observer: tuple[ObserverContract, ObserverDescriptor] | None) -> None:
        self.observer = observer

    def descriptor(self, registration_id: str) -> ObserverDescriptor:
        assert registration_id == "fixture-observer"
        assert self.observer is not None
        return self.observer[1]

    def observe(
        self,
        registration_id: str,
        invocation: object,
        *,
        descriptor: ObserverDescriptor,
    ) -> ObservationResult:
        assert registration_id == "fixture-observer"
        assert self.observer is not None
        invocation = ObservationInvocation.model_validate(invocation)
        contract, expected_descriptor = self.observer
        assert descriptor == expected_descriptor
        facts = {"kind": "fixture"}
        return ObservationResult.seal(
            ObservationResultPayload(
                request_id=invocation.request.request_id,
                state="observed",
                observer=ObserverImplementation(
                    id=descriptor.implementation_id,
                    version=descriptor.implementation_version,
                    source_revision=descriptor.source_revision,
                    descriptor_sha256=descriptor.descriptor_sha256,
                ),
                observer_contract_id=contract.id,
                observer_contract_sha256=contract.contract_sha256,
                subjects=invocation.request.subjects,
                facts_schema=contract.facts_schema,
                facts=facts,
                facts_sha256=canonical_json_sha256(facts),
            )
        )


class FixtureTargetCallbacks:
    _authorities: dict[str, tuple[InMemoryWorkStore, str]] = {}

    def __init__(self, store: InMemoryWorkStore) -> None:
        self.store = store

    def issue_access(
        self,
        record: WorkRecord,
        _target_registration_id: str,
    ) -> TargetCallbackAccess:
        assert record.controller_evidence is not None
        token = "fixture-" + record.controller_evidence.execution_envelope.execution_envelope_sha256
        self._authorities[token] = (self.store, record.work_id)
        return TargetCallbackAccess(
            stove0_base_url="https://stove0.invalid",
            token=token,
        )

    @classmethod
    def record_production(
        cls,
        request: TargetJobRequest | AcceptedTargetJob,
        output: OutputArtifact,
    ) -> None:
        if not isinstance(request, TargetJobRequest):
            return
        store, work_id = cls._authorities[request.callback_access.token]
        job_id = request.declaration.job_id
        store.record_target_output(work_id, job_id, output)
        selection_sha256 = request.declaration.plan.inputs.selection.selection_sha256
        for subject in store.iter_selection_artifacts(selection_sha256):
            store.record_target_disposition(
                work_id,
                job_id,
                InputDispositionDeclaration(input_id=subject.id, status="transformed"),
            )
            store.record_target_source_edge(
                work_id,
                job_id,
                OutputSourceEdge(output_id=output.id, input_id=subject.id),
            )


def _successful_target_status(
    target: TargetContract,
    operation: OperationContract,
    request: TargetJobRequest | AcceptedTargetJob,
    *,
    output_id: str,
    output_role: str,
    output_path: str,
    output_sha256: str,
    collection_id: int,
    archive_root_sha256: str,
    content_identity: str,
) -> TargetJobStatus:
    declaration = request.declaration
    workflow = declaration.controller_evidence.execution_envelope.workflow_plan
    output = OutputArtifact(
        id=output_id,
        role=output_role,
        path=output_path,
        bytes=12,
        sha256=output_sha256,
    )
    FixtureTargetCallbacks.record_production(request, output)
    input_count = declaration.plan.inputs.selection.artifact_count
    disposition_set = ArtifactDispositionSetIdentity(
        disposition_count=input_count,
        output_edge_count=input_count,
        output_artifact_count=1,
        sha256=canonical_json_sha256({"job_id": declaration.job_id, "authority": "dispositions"}),
    )
    derivation = CollectionDerivation(
        execution_id=declaration.job_id,
        claim_id=declaration.claim_id,
        fence=declaration.fence,
        recipe=workflow.work.recipe.to_identity(),
        operation=workflow.operation.to_identity(),
        input_set_sha256=_sha("a"),
        artifact_set_sha256=_sha("b"),
        output_tag_set_sha256=_sha("c"),
        execution_envelope_sha256=declaration.job_id,
        execution_sha256=_sha("9"),
        controller_evidence=declaration.controller_evidence.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        ),
        controller_evidence_sha256=riverhog_canonical_json_sha256(
            declaration.controller_evidence.model_dump(
                mode="json", by_alias=True, exclude_none=True
            )
        ),
        disposition_set=disposition_set,
    )
    production = TargetProductionAuthority.seal(
        TargetProductionAuthorityPayload(
            job_id=declaration.job_id,
            plan_sha256=declaration.plan.plan_sha256,
            outputs=OutputArtifactSetIdentity.seal((output,)),
            disposition_count=input_count,
            disposition_sha256=canonical_json_sha256(
                {"job_id": declaration.job_id, "authority": "target-dispositions"}
            ),
            source_edge_count=input_count,
            source_edge_sha256=canonical_json_sha256(
                {"job_id": declaration.job_id, "authority": "source-edges"}
            ),
            riverhog_disposition_set=disposition_set,
        )
    )
    return TargetJobStatus(
        job_id=declaration.job_id,
        state="succeeded",
        attempt=1,
        request_sha256=request.request_sha256,
        plan_sha256=declaration.plan.plan_sha256,
        progress=TargetProgress(phase="done", completed=1, total=1),
        production=production,
        output_collection=OutputCollectionRef(
            collection_id=collection_id,
            archive_root_sha256=archive_root_sha256,
            content_identity=content_identity,
            derivation_sha256=derivation.sha256,
        ),
        execution_evidence=TargetExecutionEvidence(
            target_contract_sha256=target.contract_sha256,
            operation_contract_sha256=operation.contract_sha256,
            plan_sha256=declaration.plan.plan_sha256,
            execution_sha256=_sha("9"),
        ),
        derivation=derivation.as_dict(),
    )


def _target_settlement(status: TargetJobStatus) -> TargetSettlementAuthority:
    assert status.production is not None
    assert status.output_collection is not None
    return TargetSettlementAuthority.seal(
        TargetSettlementAuthorityPayload(
            job_id=status.job_id,
            production_sha256=status.production.production_sha256,
            output_collection=status.output_collection,
            output_bindings=TargetOutputBindingSetIdentity(
                artifact_count=status.production.outputs.artifact_count,
                total_bytes=status.production.outputs.total_bytes,
                sha256=canonical_json_sha256(
                    {"job_id": status.job_id, "authority": "output-bindings"}
                ),
            ),
        )
    )


class FixtureTarget:
    def __init__(self, operation: OperationContract, target: TargetContract) -> None:
        self.operation = operation
        self.target = target
        self.accepted: TargetJobRequest | None = None
        self.jobs: dict[str, TargetJobRequest] = {}

    def contract(self, registration_id: str) -> TargetContract:
        assert registration_id == "fixture-target"
        return self.target

    def preflight(
        self,
        registration_id: str,
        request: TargetPreflightRequest,
    ) -> TargetPreflightResponse:
        assert registration_id == "fixture-target"
        plan = TransformPlan.seal(
            TransformPlanPayload(
                target_implementation_id=self.target.implementation_id,
                target_contract_sha256=self.target.contract_sha256,
                operation_id=request.operation_id,
                operation_contract_sha256=request.operation_contract_sha256,
                inputs=request.inputs,
                intent=request.intent,
                target_options=request.target_options,
                observation_result_sha256s=tuple(
                    sorted(item.result.result_sha256 for item in request.observations)
                ),
            )
        )
        return TargetPreflightResponse(target=self.target, plan=plan)

    def put_job(
        self,
        registration_id: str,
        request: TargetJobRequest,
        *,
        operation: OperationContract,
    ) -> TargetJobStatus:
        assert registration_id == "fixture-target"
        assert operation.id == request.declaration.plan.operation_id
        job_id = request.declaration.job_id
        existing = self.jobs.get(job_id)
        if existing is None:
            self.jobs[job_id] = request
            self.accepted = request
            return TargetJobStatus(
                job_id=job_id,
                state="running",
                attempt=1,
                request_sha256=request.request_sha256,
                plan_sha256=request.declaration.plan.plan_sha256,
                progress=TargetProgress(phase="transform", completed=0, total=1),
            )
        assert request.accepted() == existing.accepted()
        self.jobs[job_id] = request
        self.accepted = request
        return self.get_job(registration_id, request, operation=operation)

    def cancel_job(
        self,
        registration_id: str,
        request: TargetJobRequest | AcceptedTargetJob,
        *,
        operation: OperationContract,
    ) -> TargetJobStatus:
        assert registration_id == "fixture-target"
        assert operation.id == request.declaration.plan.operation_id
        job_id = request.declaration.job_id
        accepted = request.accepted() if isinstance(request, TargetJobRequest) else request
        assert self.jobs[job_id].accepted() == accepted
        return TargetJobStatus(
            job_id=job_id,
            state="canceled",
            attempt=1,
            request_sha256=request.request_sha256,
            plan_sha256=request.declaration.plan.plan_sha256,
            progress=TargetProgress(phase="canceled", completed=0, total=1),
        )

    def get_job(
        self,
        registration_id: str,
        request: TargetJobRequest | AcceptedTargetJob,
        *,
        operation: OperationContract,
    ) -> TargetJobStatus:
        assert registration_id == "fixture-target"
        assert operation.id == request.declaration.plan.operation_id
        job_id = request.declaration.job_id
        accepted = request.accepted() if isinstance(request, TargetJobRequest) else request
        assert self.jobs[job_id].accepted() == accepted
        return _successful_target_status(
            self.target,
            self.operation,
            request,
            output_id="output",
            output_role="fixture.output/v1",
            output_path="output/result.bin",
            output_sha256=_sha("5"),
            collection_id=7,
            archive_root_sha256=_sha("6"),
            content_identity=_sha("7"),
        )


class DriftingPreflightTarget(FixtureTarget):
    def preflight(
        self,
        registration_id: str,
        request: TargetPreflightRequest,
    ) -> TargetPreflightResponse:
        response = super().preflight(registration_id, request)
        changed = TransformPlan.seal(
            TransformPlanPayload(
                **response.plan.model_dump(
                    mode="python",
                    exclude={"plan_sha256", "target_options"},
                ),
                target_options={**response.plan.target_options, "runtime-profile": "changed"},
            )
        )
        return TargetPreflightResponse(target=self.target, plan=changed)


class ForkJoinTarget(FixtureTarget):
    def __init__(
        self,
        operations: tuple[OperationContract, OperationContract],
        target: TargetContract,
    ) -> None:
        super().__init__(operations[0], target)
        self.operations = {operation.id: operation for operation in operations}

    def get_job(
        self,
        registration_id: str,
        request: TargetJobRequest | AcceptedTargetJob,
        *,
        operation: OperationContract,
    ) -> TargetJobStatus:
        assert registration_id == "fixture-target"
        assert operation.id == request.declaration.plan.operation_id
        job_id = request.declaration.job_id
        accepted = request.accepted() if isinstance(request, TargetJobRequest) else request
        assert self.jobs[job_id].accepted() == accepted
        declaration = request.declaration
        workflow = declaration.controller_evidence.execution_envelope.workflow_plan
        operation = self.operations[workflow.operation.id]
        binding = workflow.work.fork_join
        if isinstance(binding, BranchWorkBinding):
            output_id = f"{binding.branch_id}-output"
            output_role = "fixture.branch-output/v1"
        elif isinstance(binding, JoinWorkBinding):
            output_id = "joined-output"
            output_role = "fixture.joined-output/v1"
        else:
            raise AssertionError("fork/join target received unbound work")
        output_path = f"output/{output_id}.bin"
        return _successful_target_status(
            self.target,
            operation,
            request,
            output_id=output_id,
            output_role=output_role,
            output_path=output_path,
            output_sha256=job_id,
            collection_id=100 + int(workflow.work.work_id[:12], 16) % 1_000_000_000,
            archive_root_sha256=workflow.work.work_id,
            content_identity=workflow.workflow_plan_sha256,
        )


class FixtureRiverhog:
    def __init__(self) -> None:
        self.sealed = False
        self.released = False
        self.abandoned = False
        self.renewals = 0
        self.target_authorities = 0
        self.target: FixtureTarget | None = None
        self.renewed_claim: ClaimBinding | None = None
        self.restarted = False
        self.claims: dict[str, ClaimBinding] = {}
        self.outcomes: dict[str, OutputCollectionRef] = {}
        self.coordination_settled = False

    def acquire_claim(self, work: WorkIdentity) -> ClaimBinding:
        return self.claims.setdefault(
            work.work_id,
            ClaimBinding(claim_id=f"claim-{len(self.claims) + 1}", fence=1),
        )

    def renew_claim(self, _work: WorkIdentity, claim: ClaimBinding) -> ClaimBinding:
        self.renewals += 1
        return self.renewed_claim or claim

    def restart_claim(self, work: WorkIdentity, claim: ClaimBinding) -> ClaimBinding:
        self.restarted = True
        replacement = ClaimBinding(claim_id=claim.claim_id, fence=claim.fence + 1)
        self.claims[work.work_id] = replacement
        return replacement

    def observation_authority(
        self,
        _claim: ClaimBinding,
        _request: ObservationRequest,
    ) -> ObserverRuntimeAuthority:
        return ObserverRuntimeAuthority(
            riverhog_base_url="https://riverhog.invalid",
            capability_token="observer-secret",
            workspace_assurance="ephemeral",
        )

    def seal_execution(
        self,
        _claim: ClaimBinding,
        _evidence: object,
        _plan: WorkflowPlan,
        target_plan: TransformPlan,
        _artifacts: object,
    ) -> None:
        assert target_plan.inputs
        self.sealed = True

    def target_authority(
        self,
        _claim: ClaimBinding,
        _evidence: object,
        target_plan: TransformPlan,
        _artifacts: object,
    ) -> TargetInvocationAuthority:
        assert target_plan.inputs
        self.target_authorities += 1
        return TargetInvocationAuthority(
            runtime=TargetRuntimeAuthority(
                riverhog_base_url="https://riverhog.invalid",
                capability_token=f"target-secret-{self.target_authorities}",
            ),
            workspace_assurance="ephemeral",
        )

    def verify_and_settle(
        self,
        record: object,
        parent_outcome: ParentOutcomeBinding | None = None,
    ) -> tuple[OutputCollectionRef, TargetSettlementAuthority]:
        from stove0_core import WorkRecord

        record = WorkRecord.model_validate(record)
        assert record.target_status is not None
        assert record.target_status.output_collection is not None
        output = record.target_status.output_collection
        if parent_outcome is not None:
            assert parent_outcome.claim in self.claims.values()
            existing = self.outcomes.setdefault(parent_outcome.outcome_id, output)
            assert existing == output
        return output, _target_settlement(record.target_status)

    def settle_outcomes(
        self,
        record: object,
        evaluation: BranchSetEvaluation,
    ) -> bool:
        from stove0_core import WorkRecord

        record = WorkRecord.model_validate(record)
        assert record.branch_set_plan is not None
        assert evaluation.branch_set_succeeded
        assert set(self.outcomes) == {
            *(f"branch/{item.branch_id}" for item in evaluation.succeeded_branches),
            *({"join"} if evaluation.join_settlement is not None else set()),
        }
        self.coordination_settled = True
        return True

    def abandon_claim(self, _record: object) -> None:
        self.abandoned = True

    def begin_retirement(self, _record: object) -> None:
        raise AssertionError("retain policy must not begin retirement")

    def retire_input(self, _record: object, _collection_id: int) -> None:
        raise AssertionError("retain policy must not retire inputs")

    def release_claim(self, _record: object) -> None:
        self.released = True


class RetirementWaitingRiverhog(FixtureRiverhog):
    def __init__(self) -> None:
        super().__init__()
        self.begin_ready = False
        self.deletion_ready = False

    def begin_retirement(self, _record: object) -> bool:
        return self.begin_ready

    def retire_input(self, _record: object, _collection_id: int) -> bool:
        return self.deletion_ready


class DelayedSettlementRiverhog(FixtureRiverhog):
    def __init__(self) -> None:
        super().__init__()
        self.binding_ready = False

    def verify_and_settle(
        self,
        record: object,
        parent_outcome: ParentOutcomeBinding | None = None,
    ) -> tuple[OutputCollectionRef, TargetSettlementAuthority | None]:
        current = WorkRecord.model_validate(record)
        assert current.target_status is not None
        assert current.target_status.output_collection is not None
        if not self.binding_ready:
            return current.target_status.output_collection, None
        return super().verify_and_settle(current, parent_outcome)


class NestedFixtureRiverhog(FixtureRiverhog):
    def __init__(self) -> None:
        super().__init__()
        self.scoped_outcomes: dict[str, dict[str, OutputCollectionRef]] = {}
        self.settled_claims: set[str] = set()

    def verify_and_settle(
        self,
        record: object,
        parent_outcome: ParentOutcomeBinding | None = None,
    ) -> tuple[OutputCollectionRef, TargetSettlementAuthority]:
        record = WorkRecord.model_validate(record)
        assert record.target_status is not None
        assert record.target_status.output_collection is not None
        output = record.target_status.output_collection
        if parent_outcome is not None:
            outcomes = self.scoped_outcomes.setdefault(parent_outcome.claim.claim_id, {})
            existing = outcomes.setdefault(parent_outcome.outcome_id, output)
            assert existing == output
        return output, _target_settlement(record.target_status)

    def settle_outcomes(
        self,
        record: object,
        evaluation: BranchSetEvaluation,
    ) -> bool:
        record = WorkRecord.model_validate(record)
        assert record.claim is not None
        expected = {
            *(f"branch/{item.branch_id}" for item in evaluation.succeeded_branches),
            *({"join"} if evaluation.join_settlement is not None else set()),
        }
        assert set(self.scoped_outcomes.get(record.claim.claim_id, {})) == expected
        self.settled_claims.add(record.claim.claim_id)
        return True


def _workflow_preview(
    planning: FixturePlanning,
    target: FixtureTarget,
) -> WorkflowPreview:
    work = _work()
    decision = planning.workflow_plan(work, ())
    target_plans: list[BranchTargetPreview] = []
    for branch in decision.plan.branches:
        workflow = branch.workflow_plan
        request = planning.target_preflight_request(
            workflow,
            decision.selection_documents,
        )
        response = target.preflight(workflow.target_registration_id, request)
        plan = response.plan
        target_plans.append(
            BranchTargetPreview(
                branch_id=branch.branch_id,
                work_id=workflow.work.work_id,
                workflow_plan_sha256=workflow.workflow_plan_sha256,
                target_plan=TargetPlanBinding(
                    protocol=response.target.protocol,
                    target_implementation_id=response.target.implementation_id,
                    target_contract_sha256=response.target.contract_sha256,
                    operation_contract_sha256=plan.operation_contract_sha256,
                    plan=plan.binding_document(),
                    plan_sha256=plan.plan_sha256,
                ),
            )
        )
    request = WorkflowPreviewRequest.seal(WorkflowPreviewRequestPayload(work=work))
    return WorkflowPreview.seal(
        WorkflowPreviewPayload(
            preview_id=request.preview_id,
            state="ready",
            work=work,
            branch_set_plan=decision.plan,
            selections=decision.selections,
            target_plans=tuple(target_plans),
        )
    )


def _run(observer_enabled: bool) -> tuple[object, object, FixtureRiverhog, FixtureTarget]:
    operation = _operation()
    target_contract = _target(operation)
    observer = _observer() if observer_enabled else None
    store = InMemoryWorkStore()
    state = Stove0WorkService(store)
    riverhog = FixtureRiverhog()
    target = FixtureTarget(operation, target_contract)
    coordinator = Stove0Coordinator(
        state,
        riverhog=riverhog,
        planning=FixturePlanning(operation, target_contract, observer),
        observers=FixtureObservers(observer),
        targets=target,
        target_callbacks=FixtureTargetCallbacks(store),
    )
    parent = coordinator.create_or_resume(_work())
    child = None
    for _ in range(24):
        if parent.phase == "complete":
            break
        if parent.phase != "coordinating":
            parent = coordinator.step(parent.work_id)
            continue
        assert parent.branch_set_plan is not None
        child_id = parent.branch_set_plan.branches[0].workflow_plan.work.work_id
        child = store.load(child_id)
        assert child is not None
        if child.phase != "complete":
            child = coordinator.step(child.work_id)
        else:
            parent = coordinator.step(parent.work_id)
    assert child is not None
    return parent, child, riverhog, target


def _advance_child_to(
    coordinator: Stove0Coordinator,
    store: InMemoryWorkStore,
    phase: str,
):  # type: ignore[no-untyped-def]
    parent = coordinator.create_or_resume(_work())
    while parent.phase != "coordinating":
        parent = coordinator.step(parent.work_id)
    assert parent.branch_set_plan is not None
    child_id = parent.branch_set_plan.branches[0].workflow_plan.work.work_id
    child = store.load(child_id)
    assert child is not None
    while child.phase != phase:
        child = coordinator.step(child.work_id)
    return parent, child


def test_operator_initiation_binds_the_exact_preview_and_target_plan() -> None:
    operation = _operation()
    target_contract = _target(operation)
    planning = FixturePlanning(operation, target_contract, None)
    target = FixtureTarget(operation, target_contract)
    preview = _workflow_preview(planning, target)
    store = InMemoryWorkStore()
    coordinator = Stove0Coordinator(
        Stove0WorkService(store),
        riverhog=FixtureRiverhog(),
        planning=planning,
        observers=FixtureObservers(None),
        targets=target,
        target_callbacks=FixtureTargetCallbacks(store),
    )

    parent = coordinator.create_or_resume(_work(), preview=preview)
    assert parent.preview_acceptance is not None
    assert parent.preview_acceptance.preview_sha256 == preview.preview_sha256
    while parent.phase != "coordinating":
        parent = coordinator.step(parent.work_id)
    assert parent.branch_set_plan is not None
    child = store.load(parent.branch_set_plan.branches[0].workflow_plan.work.work_id)
    assert child is not None
    assert child.expected_target_plan_sha256 == preview.target_plans[0].target_plan.plan_sha256
    while child.phase != "queued":
        child = coordinator.step(child.work_id)
    assert child.target_plan is not None
    assert child.target_plan.plan_sha256 == child.expected_target_plan_sha256


def test_operator_initiation_fails_truthfully_when_target_preflight_changes() -> None:
    operation = _operation()
    target_contract = _target(operation)
    planning = FixturePlanning(operation, target_contract, None)
    preview = _workflow_preview(planning, FixtureTarget(operation, target_contract))
    store = InMemoryWorkStore()
    coordinator = Stove0Coordinator(
        Stove0WorkService(store),
        riverhog=FixtureRiverhog(),
        planning=planning,
        observers=FixtureObservers(None),
        targets=DriftingPreflightTarget(operation, target_contract),
        target_callbacks=FixtureTargetCallbacks(store),
    )
    parent = coordinator.create_or_resume(_work(), preview=preview)
    while parent.phase != "coordinating":
        parent = coordinator.step(parent.work_id)
    assert parent.branch_set_plan is not None
    child = store.load(parent.branch_set_plan.branches[0].workflow_plan.work.work_id)
    assert child is not None
    while child.phase != "failed":
        child = coordinator.step(child.work_id)

    assert child.failure is not None
    assert child.failure.code == "accepted-preview-target-changed"
    assert child.failure.retryable is True
    assert child.target_request is None


def test_coordinator_executes_two_retained_branches_and_one_exact_final_join() -> None:
    operations = _fork_join_operations()
    target_contract = _fork_join_target(operations)
    planning = ForkJoinPlanning(*operations, target_contract)
    target = ForkJoinTarget(operations, target_contract)
    store = InMemoryWorkStore()
    riverhog = FixtureRiverhog()
    coordinator = Stove0Coordinator(
        Stove0WorkService(store),
        riverhog=riverhog,
        planning=planning,
        observers=FixtureObservers(None),
        targets=target,
        target_callbacks=FixtureTargetCallbacks(store),
    )
    parent = coordinator.create_or_resume(_work())

    for _ in range(128):
        parent = store.load(parent.work_id)
        assert parent is not None
        if parent.phase == "complete":
            break
        if parent.phase != "coordinating":
            coordinator.step(parent.work_id)
            continue
        assert parent.branch_set_plan is not None
        child_ids = [
            branch.workflow_plan.work.work_id for branch in parent.branch_set_plan.branches
        ]
        if parent.join_plan is not None:
            child_ids.append(parent.join_plan.work.work_id)
        for child_id in child_ids:
            child = store.load(child_id)
            assert child is not None
            if child.phase != "complete":
                coordinator.step(child_id)
        coordinator.step(parent.work_id)

    parent = store.load(parent.work_id)
    assert parent is not None and parent.phase == "complete"
    assert parent.branch_set_plan is not None and parent.join_plan is not None
    assert riverhog.coordination_settled is True
    assert set(riverhog.outcomes) == {"branch/audio", "branch/video", "join"}
    assert len({item.collection_id for item in riverhog.outcomes.values()}) == 3
    join = store.load(parent.join_plan.work.work_id)
    assert join is not None and join.phase == "complete" and join.output is not None
    assert join.output == riverhog.outcomes["join"]


def test_coordination_settles_parent_only_after_successful_children_complete() -> None:
    operation = _operation()
    target_contract = _target(operation)
    store = InMemoryWorkStore()
    riverhog = FixtureRiverhog()
    coordinator = Stove0Coordinator(
        Stove0WorkService(store),
        riverhog=riverhog,
        planning=FixturePlanning(operation, target_contract, None),
        observers=FixtureObservers(None),
        targets=FixtureTarget(operation, target_contract),
        target_callbacks=FixtureTargetCallbacks(store),
    )
    parent, child = _advance_child_to(coordinator, store, "settled")

    unchanged = coordinator.step(parent.work_id)

    assert unchanged.phase == "coordinating"
    assert riverhog.coordination_settled is False

    child = coordinator.step(child.work_id)
    assert child.phase == "complete"
    completed = coordinator.step(parent.work_id)

    assert completed.phase == "complete"
    assert riverhog.coordination_settled is True


def test_incomplete_post_root_binding_is_not_visible_to_coordination() -> None:
    operation = _operation()
    target_contract = _target(operation)
    store = InMemoryWorkStore()
    riverhog = DelayedSettlementRiverhog()
    coordinator = Stove0Coordinator(
        Stove0WorkService(store),
        riverhog=riverhog,
        planning=FixturePlanning(operation, target_contract, None),
        observers=FixtureObservers(None),
        targets=FixtureTarget(operation, target_contract),
        target_callbacks=FixtureTargetCallbacks(store),
    )
    parent, child = _advance_child_to(coordinator, store, "verifying")

    waiting = coordinator.step(child.work_id)

    assert waiting.phase == "verifying"
    assert waiting.target_settlement is None
    assert coordinator.inspect_coordination(parent.work_id).succeeded_branches == ()
    assert riverhog.outcomes == {}

    riverhog.binding_ready = True
    settled = coordinator.step(child.work_id)

    assert settled.phase == "settled"
    assert settled.target_settlement is not None
    assert coordinator.inspect_coordination(parent.work_id).succeeded_branches


def test_retirement_grace_and_deletion_blockers_leave_work_stably_waiting() -> None:
    operation = _operation(source_retirement_permitted=True)
    target = _target(operation)
    work = _work()
    workflow = WorkflowPlan.seal(
        WorkflowPlanPayload(
            work=work,
            operation=OperationRef(id=operation.id, sha256=operation.contract_sha256),
            target_registration_id="fixture-target",
            target_contract_sha256=target.contract_sha256,
            output_tags=("fixture-output",),
            retirement_policy="retire-after-verified-output",
        )
    )
    store = InMemoryWorkStore()
    store.create(
        WorkRecord(
            work=work,
            phase="settled",
            revision=7,
            claim=ClaimBinding(claim_id="claim-1", fence=1),
            workflow_plan=workflow,
        )
    )
    riverhog = RetirementWaitingRiverhog()
    coordinator = Stove0Coordinator(
        Stove0WorkService(store),
        riverhog=riverhog,
        planning=FixturePlanning(operation, target, None),
        observers=FixtureObservers(None),
        targets=FixtureTarget(operation, target),
        target_callbacks=FixtureTargetCallbacks(store),
    )

    grace_waiting = coordinator.step(work.work_id)
    assert grace_waiting.phase == "settled"
    assert grace_waiting.revision == 7

    riverhog.begin_ready = True
    retirement = coordinator.step(work.work_id)
    assert retirement.phase == "retirement_pending"
    blocked = coordinator.step(work.work_id)
    assert blocked.phase == "retirement_pending"
    assert blocked.revision == retirement.revision

    riverhog.deletion_ready = True
    complete = coordinator.step(work.work_id)
    assert complete.phase == "complete"
    assert riverhog.released is True


def test_failed_branch_waits_for_independent_sibling_then_retries_same_graph() -> None:
    operations = _fork_join_operations()
    target_contract = _fork_join_target(operations)
    store = InMemoryWorkStore()
    state = Stove0WorkService(store)
    riverhog = FixtureRiverhog()
    coordinator = Stove0Coordinator(
        state,
        riverhog=riverhog,
        planning=ForkJoinPlanning(*operations, target_contract),
        observers=FixtureObservers(None),
        targets=ForkJoinTarget(operations, target_contract),
        target_callbacks=FixtureTargetCallbacks(store),
    )
    parent = coordinator.create_or_resume(_work())
    while parent.phase != "coordinating":
        parent = coordinator.step(parent.work_id)
    assert parent.branch_set_plan is not None
    branch_set = parent.branch_set_plan
    failed_id, sibling_id = (branch.workflow_plan.work.work_id for branch in branch_set.branches)
    failed = coordinator.step(failed_id)
    failed = state.fail(
        failed_id,
        WorkFailure(code="temporary", message="Retry this branch", retryable=True),
        expected_revision=failed.revision,
    )

    waiting = coordinator.step(parent.work_id)
    assert waiting.phase == "coordinating"
    sibling = store.load(sibling_id)
    assert sibling is not None and sibling.phase == "eligible"
    for _ in range(12):
        if sibling.phase == "complete":
            break
        sibling = coordinator.step(sibling_id)
    assert sibling.phase == "complete"
    assert parent.join_plan is None

    failed_parent = coordinator.step(parent.work_id)
    assert failed_parent.phase == "failed"
    assert failed_parent.failure is not None and failed_parent.failure.retryable is True
    retried_parent = coordinator.retry(parent.work_id)
    assert retried_parent.phase == "coordinating"
    assert retried_parent.branch_set_plan == branch_set
    retried_child = store.load(failed_id)
    assert retried_child is not None
    assert retried_child.phase == "claimed"
    assert retried_child.work_id == failed.work_id
    assert retried_child.claim is not None and retried_child.claim.fence == 2

    for _ in range(12):
        if retried_child.phase == "complete":
            break
        retried_child = coordinator.step(failed_id)
    assert retried_child.phase == "complete"
    parent = coordinator.step(parent.work_id)
    assert parent.join_plan is not None
    join_id = parent.join_plan.work.work_id
    join = store.load(join_id)
    assert join is not None
    for _ in range(12):
        if join.phase == "complete":
            break
        join = coordinator.step(join_id)
    assert join.phase == "complete"
    completed = coordinator.step(parent.work_id)
    assert completed.coordination_settlement is not None
    completed = coordinator.step(parent.work_id)
    assert completed.phase == "complete"
    assert completed.branch_set_plan == branch_set


def test_nested_coordination_executes_as_normalized_work_and_seals_exact_settlements() -> None:
    operation = _operation()
    target_contract = _target(operation)
    store = InMemoryWorkStore()
    state = Stove0WorkService(store)
    riverhog = NestedFixtureRiverhog()
    coordinator = Stove0Coordinator(
        state,
        riverhog=riverhog,
        planning=NestedPlanning(operation, target_contract, None),
        observers=FixtureObservers(None),
        targets=FixtureTarget(operation, target_contract),
        target_callbacks=FixtureTargetCallbacks(store),
    )
    root = coordinator.create_or_resume(_work())
    while root.phase != "coordinating":
        root = coordinator.step(root.work_id)
    assert root.branch_set_plan is not None
    nested = root.branch_set_plan.branches[0]
    assert isinstance(nested, CoordinationBranchPlan)
    child = store.load(nested.work.work_id)
    assert child is not None and child.branch_set_plan is not None
    child = coordinator.step(child.work_id)
    child = coordinator.step(child.work_id)
    assert child.phase == "coordinating"
    leaf = child.branch_set_plan.branches[0]
    assert isinstance(leaf, BranchPlan)
    leaf_record = store.load(leaf.workflow_plan.work.work_id)
    assert leaf_record is not None
    for _ in range(12):
        if leaf_record.phase == "complete":
            break
        leaf_record = coordinator.step(leaf_record.work_id)
    assert leaf_record.phase == "complete"

    child = coordinator.step(child.work_id)
    assert child.phase == "coordinating"
    assert child.coordination_settlement is not None
    assert child.coordination_settlement.collection_result is None
    assert [item.kind for item in child.coordination_settlement.children] == ["collection"]
    child = coordinator.step(child.work_id)
    assert child.phase == "complete"

    root = coordinator.step(root.work_id)
    assert root.phase == "coordinating"
    assert root.coordination_settlement is not None
    assert root.coordination_settlement.collection_result is None
    assert [item.kind for item in root.coordination_settlement.children] == ["coordination"]
    root = coordinator.step(root.work_id)
    assert root.phase == "complete"
    inspected = coordinator.inspect_coordination(root.work_id)
    assert inspected.branch_set_succeeded is True
    assert inspected.succeeded_coordinations == (child.coordination_settlement,)


def test_nested_final_join_exposes_actual_join_collection_without_relabeling_producer() -> None:
    operations = _fork_join_operations()
    target_contract = _fork_join_target(operations)
    store = InMemoryWorkStore()
    state = Stove0WorkService(store)
    coordinator = Stove0Coordinator(
        state,
        riverhog=NestedFixtureRiverhog(),
        planning=NestedJoinPlanning(*operations, target_contract),
        observers=FixtureObservers(None),
        targets=ForkJoinTarget(operations, target_contract),
        target_callbacks=FixtureTargetCallbacks(store),
    )
    root = coordinator.create_or_resume(_work())
    while root.phase != "coordinating":
        root = coordinator.step(root.work_id)
    assert root.branch_set_plan is not None
    nested = root.branch_set_plan.branches[0]
    assert isinstance(nested, CoordinationBranchPlan)
    child = store.load(nested.work.work_id)
    assert child is not None and child.branch_set_plan is not None
    child = coordinator.step(child.work_id)
    child = coordinator.step(child.work_id)
    assert child.phase == "coordinating"

    for declaration in child.branch_set_plan.branches:
        assert isinstance(declaration, BranchPlan)
        leaf = store.load(declaration.workflow_plan.work.work_id)
        assert leaf is not None
        for _ in range(12):
            if leaf.phase == "complete":
                break
            leaf = coordinator.step(leaf.work_id)
        assert leaf.phase == "complete"
    child = coordinator.step(child.work_id)
    assert child.join_plan is not None
    join_work_id = child.join_plan.work.work_id
    join = store.load(join_work_id)
    assert join is not None
    for _ in range(12):
        if join.phase == "complete":
            break
        join = coordinator.step(join.work_id)
    assert join.phase == "complete"

    child = coordinator.step(child.work_id)
    settlement = child.coordination_settlement
    assert settlement is not None and settlement.collection_result is not None
    assert settlement.collection_result.producer_work_id == join_work_id
    assert settlement.collection_result.output_collection.collection_id == (
        join.output.collection_id
    )
    assert settlement.collection_result.join_settlement_sha256 == (
        settlement.final_join_settlement_sha256
    )
    child = coordinator.step(child.work_id)
    assert child.phase == "complete"

    root = coordinator.step(root.work_id)
    assert root.coordination_settlement is not None
    assert root.coordination_settlement.collection_result is None
    root = coordinator.step(root.work_id)
    assert root.phase == "complete"


def test_parent_cancellation_propagates_through_unclaimed_nested_subtree() -> None:
    operation = _operation()
    target_contract = _target(operation)
    store = InMemoryWorkStore()
    coordinator = Stove0Coordinator(
        Stove0WorkService(store),
        riverhog=NestedFixtureRiverhog(),
        planning=NestedPlanning(operation, target_contract, None),
        observers=FixtureObservers(None),
        targets=FixtureTarget(operation, target_contract),
        target_callbacks=FixtureTargetCallbacks(store),
    )
    root = coordinator.create_or_resume(_work())
    while root.phase != "coordinating":
        root = coordinator.step(root.work_id)
    assert root.branch_set_plan is not None
    nested = root.branch_set_plan.branches[0]
    assert isinstance(nested, CoordinationBranchPlan)
    child = store.load(nested.work.work_id)
    assert child is not None and child.branch_set_plan is not None
    leaf = child.branch_set_plan.branches[0]
    assert isinstance(leaf, BranchPlan)

    root = coordinator.cancel(root.work_id)
    assert root.coordination_cancel_requested is True
    coordinator.step(root.work_id)
    child = store.load(child.work_id)
    assert child is not None and child.coordination_cancel_requested is True
    coordinator.step(child.work_id)
    leaf_record = store.load(leaf.workflow_plan.work.work_id)
    assert leaf_record is not None and leaf_record.phase == "canceled"
    child = coordinator.step(child.work_id)
    assert child.phase == "canceled"
    root = coordinator.step(root.work_id)
    assert root.phase == "abandon_pending"
    root = coordinator.step(root.work_id)
    assert root.phase == "canceled"


def test_interrupted_branch_remains_explicit_and_resumes_without_parent_failure() -> None:
    operation = _operation()
    target_contract = _target(operation)
    store = InMemoryWorkStore()
    state = Stove0WorkService(store)
    coordinator = Stove0Coordinator(
        state,
        riverhog=FixtureRiverhog(),
        planning=FixturePlanning(operation, target_contract, None),
        observers=FixtureObservers(None),
        targets=FixtureTarget(operation, target_contract),
        target_callbacks=FixtureTargetCallbacks(store),
    )
    parent, child = _advance_child_to(coordinator, store, "executing")
    assert child.target_request is not None
    interrupted = TargetJobStatus(
        job_id=child.target_request.declaration.job_id,
        state="interrupted",
        attempt=1,
        request_sha256=child.target_request.request_sha256,
        plan_sha256=child.target_request.declaration.plan.plan_sha256,
        progress=TargetProgress(phase="interrupted", completed=0, total=1),
    )
    child = state.record_target_status(
        child.work_id,
        interrupted,
        operation=operation,
        expected_revision=child.revision,
    )

    evaluation = coordinator.inspect_coordination(parent.work_id)
    assert evaluation.interrupted_branch_ids == ("fixture",)
    assert coordinator.step(parent.work_id).phase == "coordinating"
    resumed = coordinator.step(child.work_id)
    assert resumed.phase == "verifying"


def test_inapplicable_branch_converges_parent_and_releases_claims() -> None:
    operation = _operation()
    target_contract = _target(operation)
    store = InMemoryWorkStore()
    state = Stove0WorkService(store)
    riverhog = FixtureRiverhog()
    coordinator = Stove0Coordinator(
        state,
        riverhog=riverhog,
        planning=FixturePlanning(operation, target_contract, None),
        observers=FixtureObservers(None),
        targets=FixtureTarget(operation, target_contract),
        target_callbacks=FixtureTargetCallbacks(store),
    )
    parent, child = _advance_child_to(coordinator, store, "claimed")
    child = state.mark_inapplicable(
        child.work_id,
        WorkInapplicable(code="unsupported", message="No applicable transform"),
        expected_revision=child.revision,
    )
    assert coordinator.step(child.work_id).phase == "inapplicable"

    pending = coordinator.step(parent.work_id)
    assert pending.phase == "abandon_pending"
    terminal = coordinator.step(parent.work_id)
    assert terminal.phase == "inapplicable"
    assert terminal.inapplicable is not None
    assert terminal.inapplicable.code == "branch-set-inapplicable"
    assert riverhog.abandoned is True


def test_canceled_branch_converges_parent_and_releases_claims() -> None:
    operation = _operation()
    target_contract = _target(operation)
    store = InMemoryWorkStore()
    state = Stove0WorkService(store)
    riverhog = FixtureRiverhog()
    coordinator = Stove0Coordinator(
        state,
        riverhog=riverhog,
        planning=FixturePlanning(operation, target_contract, None),
        observers=FixtureObservers(None),
        targets=FixtureTarget(operation, target_contract),
        target_callbacks=FixtureTargetCallbacks(store),
    )
    parent, child = _advance_child_to(coordinator, store, "claimed")
    child = state.cancel(child.work_id, expected_revision=child.revision)
    assert coordinator.step(child.work_id).phase == "canceled"

    pending = coordinator.step(parent.work_id)
    assert pending.phase == "abandon_pending"
    terminal = coordinator.step(parent.work_id)
    assert terminal.phase == "canceled"
    assert riverhog.abandoned is True


def test_failed_join_converges_parent_instead_of_renewing_forever() -> None:
    operations = _fork_join_operations()
    target_contract = _fork_join_target(operations)
    store = InMemoryWorkStore()
    state = Stove0WorkService(store)
    riverhog = FixtureRiverhog()
    coordinator = Stove0Coordinator(
        state,
        riverhog=riverhog,
        planning=ForkJoinPlanning(*operations, target_contract),
        observers=FixtureObservers(None),
        targets=ForkJoinTarget(operations, target_contract),
        target_callbacks=FixtureTargetCallbacks(store),
    )
    parent = coordinator.create_or_resume(_work())
    while parent.phase != "coordinating":
        parent = coordinator.step(parent.work_id)
    assert parent.branch_set_plan is not None
    for branch in parent.branch_set_plan.branches:
        child_id = branch.workflow_plan.work.work_id
        child = store.load(child_id)
        assert child is not None
        for _ in range(12):
            if child.phase == "complete":
                break
            child = coordinator.step(child_id)
        assert child.phase == "complete"
    parent = coordinator.step(parent.work_id)
    assert parent.join_plan is not None
    join = coordinator.step(parent.join_plan.work.work_id)
    join = state.fail(
        join.work_id,
        WorkFailure(code="invalid-join", message="Join cannot complete", retryable=False),
        expected_revision=join.revision,
    )
    assert join.phase == "abandon_pending"
    assert coordinator.step(join.work_id).phase == "failed"

    pending = coordinator.step(parent.work_id)
    assert pending.phase == "abandon_pending"
    terminal = coordinator.step(parent.work_id)
    assert terminal.phase == "failed"
    assert terminal.failure is not None and terminal.failure.retryable is False
    assert riverhog.coordination_settled is False


def test_coordinator_records_inapplicable_as_a_distinct_terminal_outcome() -> None:
    operation = _operation()
    target_contract = _target(operation)
    riverhog = FixtureRiverhog()
    store = InMemoryWorkStore()
    coordinator = Stove0Coordinator(
        Stove0WorkService(store),
        riverhog=riverhog,
        planning=InapplicablePlanning(operation, target_contract, None),
        observers=FixtureObservers(None),
        targets=FixtureTarget(operation, target_contract),
        target_callbacks=FixtureTargetCallbacks(store),
    )
    record = coordinator.create_or_resume(_work())
    for _ in range(4):
        record = coordinator.step(record.work_id)

    assert record.phase == "inapplicable"
    assert record.inapplicable == WorkInapplicable(
        code="not-applicable",
        message="fixture policy selected no transform",
    )
    assert record.output is None
    assert record.target_request is None
    assert riverhog.abandoned is True


def test_coordinator_restarts_unsettled_work_under_a_new_claim_fence() -> None:
    operation = _operation()
    target_contract = _target(operation)
    store = InMemoryWorkStore()
    state = Stove0WorkService(store)
    riverhog = FixtureRiverhog()
    coordinator = Stove0Coordinator(
        state,
        riverhog=riverhog,
        planning=FixturePlanning(operation, target_contract, None),
        observers=FixtureObservers(None),
        targets=FixtureTarget(operation, target_contract),
        target_callbacks=FixtureTargetCallbacks(store),
    )
    record = coordinator.create_or_resume(_work())
    record = coordinator.step(record.work_id)
    assert record.phase == "claimed"
    riverhog.renewed_claim = ClaimBinding(claim_id="claim-1", fence=2)

    rebound = coordinator.step(record.work_id)

    assert rebound.phase == "claimed"
    assert rebound.claim == ClaimBinding(claim_id="claim-1", fence=2)
    assert rebound.workflow_plan is None
    assert rebound.target_request is None


def test_coordinator_completes_observation_free_work_without_persisting_tokens() -> None:
    parent, child, riverhog, target = _run(False)
    assert parent.phase == "complete"
    assert child.phase == "complete"
    assert riverhog.sealed and riverhog.released
    assert target.accepted is not None
    assert riverhog.renewals > 0
    assert riverhog.target_authorities >= 2
    assert target.accepted.runtime.capability_token.endswith(str(riverhog.target_authorities))
    assert child.target_request is not None
    encoded = parent.model_dump_json() + child.model_dump_json()
    assert "target-secret" not in encoded
    assert "observer-secret" not in encoded


def test_coordinator_records_pinned_observation_evidence_before_target_execution() -> None:
    parent, child, riverhog, _target_port = _run(True)
    assert parent.phase == "complete"
    assert child.phase == "complete"
    assert riverhog.sealed and riverhog.released
    assert len(parent.observation_results) == 1
    result = parent.observation_results[0]
    assert result.state == "observed"
    assert result.facts == {"kind": "fixture"}
    assert child.workflow_plan is not None
    assert (
        tuple(item.result for item in child.workflow_plan.observations)
        == parent.observation_results
    )


def test_coordinator_verifies_the_current_fence_without_renewing_it() -> None:
    operation = _operation()
    target_contract = _target(operation)
    store = InMemoryWorkStore()
    state = Stove0WorkService(store)
    riverhog = FixtureRiverhog()
    coordinator = Stove0Coordinator(
        state,
        riverhog=riverhog,
        planning=FixturePlanning(operation, target_contract, None),
        observers=FixtureObservers(None),
        targets=FixtureTarget(operation, target_contract),
        target_callbacks=FixtureTargetCallbacks(store),
    )
    _parent, record = _advance_child_to(coordinator, store, "verifying")

    renewals = riverhog.renewals
    riverhog.renewed_claim = ClaimBinding(claim_id="claim-2", fence=2)
    settled = coordinator.step(record.work_id)

    assert settled.phase == "settled"
    assert settled.claim == ClaimBinding(claim_id="claim-2", fence=1)
    assert riverhog.renewals == renewals


def test_coordinator_retries_target_failure_under_a_fresh_claim_fence() -> None:
    operation = _operation()
    target_contract = _target(operation)
    store = InMemoryWorkStore()
    state = Stove0WorkService(store)
    riverhog = FixtureRiverhog()
    target = FixtureTarget(operation, target_contract)
    coordinator = Stove0Coordinator(
        state,
        riverhog=riverhog,
        planning=FixturePlanning(operation, target_contract, None),
        observers=FixtureObservers(None),
        targets=target,
        target_callbacks=FixtureTargetCallbacks(store),
    )
    _parent, record = _advance_child_to(coordinator, store, "executing")

    assert record.target_request is not None
    failed = TargetJobStatus(
        job_id=record.target_request.declaration.job_id,
        state="failed",
        attempt=1,
        request_sha256=record.target_request.request_sha256,
        plan_sha256=record.target_request.declaration.plan.plan_sha256,
        progress=TargetProgress(phase="failed", completed=0, total=1),
        failure={
            "code": "worker-unavailable",
            "message": "fixture transient failure",
            "retryable": True,
        },
    )
    record = state.record_target_status(
        record.work_id,
        failed,
        operation=operation,
        expected_revision=record.revision,
    )
    old_execution_id = record.target_request.declaration.job_id

    retried = coordinator.retry(record.work_id)

    assert retried.phase == "claimed"
    assert retried.claim == ClaimBinding(claim_id="claim-2", fence=2)
    assert retried.target_request is None
    assert retried.failure is None
    assert riverhog.restarted is True
    for _ in range(8):
        if retried.phase == "executing":
            break
        retried = coordinator.step(retried.work_id)
    assert retried.target_request is not None
    assert retried.target_request.declaration.job_id != old_execution_id


def test_coordinator_cancels_retryable_terminal_target_failure_by_abandoning_claim() -> None:
    operation = _operation()
    target_contract = _target(operation)
    store = InMemoryWorkStore()
    state = Stove0WorkService(store)
    riverhog = FixtureRiverhog()
    target = FixtureTarget(operation, target_contract)
    coordinator = Stove0Coordinator(
        state,
        riverhog=riverhog,
        planning=FixturePlanning(operation, target_contract, None),
        observers=FixtureObservers(None),
        targets=target,
        target_callbacks=FixtureTargetCallbacks(store),
    )
    _parent, record = _advance_child_to(coordinator, store, "executing")

    assert record.target_request is not None
    failed = TargetJobStatus(
        job_id=record.target_request.declaration.job_id,
        state="failed",
        attempt=1,
        request_sha256=record.target_request.request_sha256,
        plan_sha256=record.target_request.declaration.plan.plan_sha256,
        progress=TargetProgress(phase="failed", completed=0, total=1),
        failure={
            "code": "worker-unavailable",
            "message": "fixture transient failure",
            "retryable": True,
        },
    )
    operation_contract = coordinator.planning.operation_contract(record.workflow_plan.operation)
    record = state.record_target_status(
        record.work_id,
        failed,
        operation=operation_contract,
        expected_revision=record.revision,
    )
    assert record.phase == "failed"

    pending = coordinator.cancel(record.work_id)
    assert pending.phase == "abandon_pending"
    canceled = coordinator.step(record.work_id)
    assert canceled.phase == "canceled"
    assert riverhog.abandoned is True
    assert "operator request" not in canceled.model_dump_json()
    assert "target-secret" not in canceled.model_dump_json()
    assert riverhog.abandoned is True


def test_coordinator_propagates_target_cancellation_without_persisting_reason() -> None:
    operation = _operation()
    target_contract = _target(operation)
    store = InMemoryWorkStore()
    state = Stove0WorkService(store)
    riverhog = FixtureRiverhog()
    target = FixtureTarget(operation, target_contract)
    coordinator = Stove0Coordinator(
        state,
        riverhog=riverhog,
        planning=FixturePlanning(operation, target_contract, None),
        observers=FixtureObservers(None),
        targets=target,
        target_callbacks=FixtureTargetCallbacks(store),
    )
    _parent, record = _advance_child_to(coordinator, store, "executing")

    pending = coordinator.cancel(record.work_id)
    assert pending.phase == "abandon_pending"
    canceled = coordinator.step(record.work_id)
    assert canceled.phase == "canceled"


def test_parent_cancellation_converges_children_before_abandoning_coordination() -> None:
    operation = _operation()
    target_contract = _target(operation)
    store = InMemoryWorkStore()
    state = Stove0WorkService(store)
    riverhog = FixtureRiverhog()
    target = FixtureTarget(operation, target_contract)
    coordinator = Stove0Coordinator(
        state,
        riverhog=riverhog,
        planning=FixturePlanning(operation, target_contract, None),
        observers=FixtureObservers(None),
        targets=target,
        target_callbacks=FixtureTargetCallbacks(store),
    )
    parent, child = _advance_child_to(coordinator, store, "executing")

    requested = coordinator.cancel(parent.work_id)
    assert requested.phase == "coordinating"
    assert requested.coordination_cancel_requested is True
    waiting = coordinator.step(parent.work_id)
    assert waiting.phase == "coordinating"
    child = store.load(child.work_id)
    assert child is not None and child.phase == "abandon_pending"
    assert coordinator.step(child.work_id).phase == "canceled"
    assert coordinator.step(parent.work_id).phase == "abandon_pending"
    assert coordinator.step(parent.work_id).phase == "canceled"
    assert riverhog.abandoned is True

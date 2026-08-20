from __future__ import annotations

from riverhog_protocol.collection_workflows import (
    ArtifactDisposition,
    CollectionDerivation,
)
from riverhog_protocol.collection_workflows import (
    canonical_json_sha256 as riverhog_canonical_json_sha256,
)
from stove0_core import (
    ClaimBinding,
    InMemoryWorkStore,
    Stove0Coordinator,
    Stove0WorkService,
    TargetInvocationAuthority,
    WorkInapplicable,
)
from stove0_protocol import (
    ArtifactSubject,
    CollectionRootRef,
    JsonSchemaDocument,
    ObservationEvidence,
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
    WorkflowPlanPayload,
    WorkIdentity,
    WorkPayload,
    canonical_json_sha256,
)
from stove0_target_support import (
    InputArtifact,
    InputArtifactContract,
    OperationContract,
    OperationContractPayload,
    OutputArtifact,
    OutputArtifactContract,
    OutputCollectionRef,
    TargetCancelRequest,
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
            contracts=(ObserverContractSupport.from_contract(contract),),
        )
    )
    return contract, descriptor


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
    ) -> WorkflowPlan:
        return WorkflowPlan.seal(
            WorkflowPlanPayload(
                work=work,
                observations=observations,
                operation=OperationRef(
                    id=self.operation.id,
                    sha256=self.operation.contract_sha256,
                ),
                target_registration_id="fixture-target",
                target_contract_sha256=self.target.contract_sha256,
                output_tags=("fixture-output",),
                retirement_policy="retain",
            )
        )

    def target_preflight_request(self, _plan: WorkflowPlan) -> TargetPreflightRequest:
        return TargetPreflightRequest(
            operation_id=self.operation.id,
            operation_contract_sha256=self.operation.contract_sha256,
            inputs=(_input(),),
            intent={"suffix": ".copy"},
            target_options={},
        )

    def operation_contract(self, operation: OperationRef) -> OperationContract:
        assert operation.id == self.operation.id
        assert operation.sha256 == self.operation.contract_sha256
        return self.operation


class InapplicablePlanning(FixturePlanning):
    def workflow_plan(
        self,
        work: WorkIdentity,
        observations: tuple[ObservationEvidence, ...],
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
    ) -> ObservationResult:
        from stove0_protocol import ObservationInvocation

        assert registration_id == "fixture-observer"
        assert self.observer is not None
        invocation = ObservationInvocation.model_validate(invocation)
        contract, descriptor = self.observer
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
            )
        )
        return TargetPreflightResponse(target=self.target, plan=plan)

    def put_job(
        self,
        registration_id: str,
        request: TargetJobRequest,
    ) -> TargetJobStatus:
        assert registration_id == "fixture-target"
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
        return self.get_job(registration_id, job_id)

    def cancel_job(
        self,
        registration_id: str,
        job_id: str,
        _request: TargetCancelRequest,
    ) -> TargetJobStatus:
        assert registration_id == "fixture-target"
        request = self.jobs[job_id]
        return TargetJobStatus(
            job_id=job_id,
            state="canceled",
            attempt=1,
            request_sha256=request.request_sha256,
            plan_sha256=request.declaration.plan.plan_sha256,
            progress=TargetProgress(phase="canceled", completed=0, total=1),
        )

    def get_job(self, registration_id: str, job_id: str) -> TargetJobStatus:
        assert registration_id == "fixture-target"
        request = self.jobs[job_id]
        output = OutputArtifact(
            id="output",
            role="fixture.output/v1",
            path="output/result.bin",
            bytes=12,
            sha256=_sha("5"),
            derived_from=("source",),
        )
        declaration = request.declaration
        workflow = declaration.controller_evidence.execution_envelope.workflow_plan
        derivation = CollectionDerivation(
            execution_id=job_id,
            claim_id=declaration.claim_id,
            fence=declaration.fence,
            recipe=workflow.work.recipe.to_identity(),
            operation=workflow.operation.to_identity(),
            inputs=tuple(item.to_identity() for item in workflow.work.inputs),
            output_tags=workflow.output_tags,
            execution_envelope_sha256=job_id,
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
            dispositions=(
                ArtifactDisposition(
                    input_collection_id=_root().collection_id,
                    input_manifest_sha256=_root().manifest_sha256,
                    input_path=_input().path,
                    status="transformed",
                    outputs=(output.path,),
                ),
            ),
        )
        return TargetJobStatus(
            job_id=job_id,
            state="succeeded",
            attempt=1,
            request_sha256=request.request_sha256,
            plan_sha256=request.declaration.plan.plan_sha256,
            progress=TargetProgress(phase="done", completed=1, total=1),
            outputs=(output,),
            output_collection=OutputCollectionRef(
                collection_id=7,
                manifest_sha256=_sha("6"),
                content_etag=_sha("7"),
                derivation_sha256=derivation.sha256,
            ),
            execution_evidence=TargetExecutionEvidence(
                target_contract_sha256=self.target.contract_sha256,
                operation_contract_sha256=self.operation.contract_sha256,
                plan_sha256=request.declaration.plan.plan_sha256,
                execution_sha256=_sha("9"),
            ),
            derivation=derivation.as_dict(),
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

    def acquire_claim(self, _work: WorkIdentity) -> ClaimBinding:
        return ClaimBinding(claim_id="claim-1", fence=1)

    def renew_claim(self, _work: WorkIdentity, claim: ClaimBinding) -> ClaimBinding:
        self.renewals += 1
        return self.renewed_claim or claim

    def restart_claim(self, _work: WorkIdentity, claim: ClaimBinding) -> ClaimBinding:
        self.restarted = True
        return ClaimBinding(claim_id=claim.claim_id, fence=claim.fence + 1)

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
    ) -> None:
        self.sealed = True

    def target_authority(
        self,
        _claim: ClaimBinding,
        _evidence: object,
    ) -> TargetInvocationAuthority:
        self.target_authorities += 1
        return TargetInvocationAuthority(
            runtime=TargetRuntimeAuthority(
                riverhog_base_url="https://riverhog.invalid",
                capability_token=f"target-secret-{self.target_authorities}",
            ),
            workspace_assurance="ephemeral",
        )

    def verify_and_settle(self, record: object) -> OutputCollectionRef:
        from stove0_core import WorkRecord

        record = WorkRecord.model_validate(record)
        assert record.target_status is not None
        assert record.target_status.output_collection is not None
        return record.target_status.output_collection

    def abandon_claim(self, _record: object) -> None:
        self.abandoned = True

    def begin_retirement(self, _record: object) -> None:
        raise AssertionError("retain policy must not begin retirement")

    def retire_input(self, _record: object, _collection_id: int) -> None:
        raise AssertionError("retain policy must not retire inputs")

    def release_claim(self, _record: object) -> None:
        self.released = True


def _run(observer_enabled: bool) -> tuple[object, FixtureRiverhog, FixtureTarget]:
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
    )
    record = coordinator.create_or_resume(_work())
    for _ in range(12):
        if record.phase == "complete":
            break
        record = coordinator.step(record.work_id)
    return record, riverhog, target


def test_coordinator_records_inapplicable_as_a_distinct_terminal_outcome() -> None:
    operation = _operation()
    target_contract = _target(operation)
    riverhog = FixtureRiverhog()
    coordinator = Stove0Coordinator(
        Stove0WorkService(InMemoryWorkStore()),
        riverhog=riverhog,
        planning=InapplicablePlanning(operation, target_contract, None),
        observers=FixtureObservers(None),
        targets=FixtureTarget(operation, target_contract),
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
    state = Stove0WorkService(InMemoryWorkStore())
    riverhog = FixtureRiverhog()
    coordinator = Stove0Coordinator(
        state,
        riverhog=riverhog,
        planning=FixturePlanning(operation, target_contract, None),
        observers=FixtureObservers(None),
        targets=FixtureTarget(operation, target_contract),
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
    record, riverhog, target = _run(False)
    assert record.phase == "complete"
    assert riverhog.sealed and riverhog.released
    assert target.accepted is not None
    assert riverhog.renewals > 0
    assert riverhog.target_authorities >= 2
    assert target.accepted.runtime.capability_token.endswith(str(riverhog.target_authorities))
    assert record.target_request is not None
    encoded = record.model_dump_json()
    assert "target-secret" not in encoded
    assert "observer-secret" not in encoded


def test_coordinator_records_pinned_observation_evidence_before_target_execution() -> None:
    record, riverhog, _target_port = _run(True)
    assert record.phase == "complete"
    assert riverhog.sealed and riverhog.released
    assert len(record.observation_results) == 1
    result = record.observation_results[0]
    assert result.state == "observed"
    assert result.facts == {"kind": "fixture"}
    assert record.workflow_plan is not None
    assert (
        tuple(item.result for item in record.workflow_plan.observations)
        == record.observation_results
    )


def test_coordinator_verifies_the_current_fence_without_renewing_it() -> None:
    operation = _operation()
    target_contract = _target(operation)
    state = Stove0WorkService(InMemoryWorkStore())
    riverhog = FixtureRiverhog()
    coordinator = Stove0Coordinator(
        state,
        riverhog=riverhog,
        planning=FixturePlanning(operation, target_contract, None),
        observers=FixtureObservers(None),
        targets=FixtureTarget(operation, target_contract),
    )
    record = coordinator.create_or_resume(_work())
    while record.phase != "verifying":
        record = coordinator.step(record.work_id)

    renewals = riverhog.renewals
    riverhog.renewed_claim = ClaimBinding(claim_id="claim-1", fence=2)
    settled = coordinator.step(record.work_id)

    assert settled.phase == "settled"
    assert settled.claim == ClaimBinding(claim_id="claim-1", fence=1)
    assert riverhog.renewals == renewals


def test_coordinator_retries_target_failure_under_a_fresh_claim_fence() -> None:
    operation = _operation()
    target_contract = _target(operation)
    state = Stove0WorkService(InMemoryWorkStore())
    riverhog = FixtureRiverhog()
    target = FixtureTarget(operation, target_contract)
    coordinator = Stove0Coordinator(
        state,
        riverhog=riverhog,
        planning=FixturePlanning(operation, target_contract, None),
        observers=FixtureObservers(None),
        targets=target,
    )
    record = coordinator.create_or_resume(_work())
    while record.phase != "executing":
        record = coordinator.step(record.work_id)

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
    assert retried.claim == ClaimBinding(claim_id="claim-1", fence=2)
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
    )
    record = coordinator.create_or_resume(_work())
    while record.phase != "executing":
        record = coordinator.step(record.work_id)

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
    )
    record = coordinator.create_or_resume(_work())
    while record.phase != "executing":
        record = coordinator.step(record.work_id)

    pending = coordinator.cancel(record.work_id, reason="operator request")
    assert pending.phase == "abandon_pending"
    canceled = coordinator.step(record.work_id)
    assert canceled.phase == "canceled"
    assert riverhog.abandoned is True
    assert "operator request" not in canceled.model_dump_json()
    assert "target-secret" not in canceled.model_dump_json()

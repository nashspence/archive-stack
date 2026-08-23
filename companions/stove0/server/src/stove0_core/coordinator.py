"""Deterministic stove0 coordinator over narrow injected authorities.

This module is deliberately transport- and persistence-neutral. It owns no
payload bytes, format semantics, observer implementation, target implementation,
or Riverhog database access. Production controller and worker roles may split the
ports across processes while sharing the same durable :class:`WorkRecord`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from stove0_observer_client import ContentObserverClient
from stove0_protocol import (
    ArtifactSelection,
    BranchSetDecision,
    BranchSetEvaluation,
    BranchWorkBinding,
    ControllerEvidence,
    JoinWorkBinding,
    ObservationEvidence,
    ObservationInvocation,
    ObservationRequest,
    ObservationResult,
    ObserverDescriptor,
    ObserverRuntimeAuthority,
    OperationRef,
    WorkflowPlan,
    WorkflowPreview,
    WorkIdentity,
)
from stove0_target_client import TargetClient
from stove0_target_protocol import (
    OperationContract,
    OutputCollectionRef,
    TargetCancelRequest,
    TargetContract,
    TargetJobDeclaration,
    TargetJobRequest,
    TargetJobStatus,
    TargetPlan,
    TargetPreflightRequest,
    TargetPreflightResponse,
    TargetRuntimeAuthority,
    validate_preflight_response_against_request,
)

from stove0_core.coordination import project_coordination
from stove0_core.work_state import (
    ClaimBinding,
    Stove0WorkService,
    WorkFailure,
    WorkInapplicable,
    WorkRecord,
)

WorkspaceAssurance = Literal["encrypted", "ephemeral"]


@dataclass(frozen=True, slots=True)
class TargetInvocationAuthority:
    runtime: TargetRuntimeAuthority
    workspace_assurance: WorkspaceAssurance


@dataclass(frozen=True, slots=True)
class ParentOutcomeBinding:
    """Exact parent claim outcome receiving one verified ordinary work output."""

    claim: ClaimBinding
    outcome_id: str


class RiverhogControlPort(Protocol):
    """Riverhog claim/capability/verification authority used by stove0."""

    def acquire_claim(self, work: WorkIdentity) -> ClaimBinding: ...

    def renew_claim(self, work: WorkIdentity, claim: ClaimBinding) -> ClaimBinding: ...

    def restart_claim(self, work: WorkIdentity, claim: ClaimBinding) -> ClaimBinding: ...

    def observation_authority(
        self,
        claim: ClaimBinding,
        request: ObservationRequest,
    ) -> ObserverRuntimeAuthority: ...

    def seal_execution(
        self,
        claim: ClaimBinding,
        evidence: ControllerEvidence,
        plan: WorkflowPlan,
        target_plan: TargetPlan,
    ) -> None: ...

    def target_authority(
        self,
        claim: ClaimBinding,
        evidence: ControllerEvidence,
        target_plan: TargetPlan,
    ) -> TargetInvocationAuthority: ...

    def verify_and_settle(
        self,
        record: WorkRecord,
        parent_outcome: ParentOutcomeBinding | None = None,
    ) -> OutputCollectionRef: ...

    def settle_outcomes(
        self,
        record: WorkRecord,
        evaluation: BranchSetEvaluation,
    ) -> None: ...

    def abandon_claim(self, record: WorkRecord) -> None: ...

    def begin_retirement(self, record: WorkRecord) -> bool: ...

    def retire_input(self, record: WorkRecord, collection_id: int) -> bool: ...

    def release_claim(self, record: WorkRecord) -> None: ...


class PlanningPort(Protocol):
    """Recipe/policy authority; implementations may not inspect content bytes."""

    def observation_requests(
        self,
        work: WorkIdentity,
    ) -> tuple[ObservationRequest, ...]: ...

    def workflow_plan(
        self,
        work: WorkIdentity,
        observations: tuple[ObservationEvidence, ...],
    ) -> BranchSetDecision | WorkInapplicable: ...

    def target_preflight_request(
        self,
        plan: WorkflowPlan,
        selections: dict[str, ArtifactSelection],
    ) -> TargetPreflightRequest: ...

    def operation_contract(self, operation: OperationRef) -> OperationContract: ...


class ObserverPort(Protocol):
    def descriptor(self, registration_id: str) -> ObserverDescriptor: ...

    def observe(
        self,
        registration_id: str,
        invocation: ObservationInvocation,
    ) -> ObservationResult: ...


class TargetPort(Protocol):
    def contract(self, registration_id: str) -> TargetContract: ...

    def preflight(
        self,
        registration_id: str,
        request: TargetPreflightRequest,
    ) -> TargetPreflightResponse: ...

    def put_job(
        self,
        registration_id: str,
        request: TargetJobRequest,
    ) -> TargetJobStatus: ...

    def get_job(self, registration_id: str, job_id: str) -> TargetJobStatus: ...

    def cancel_job(
        self,
        registration_id: str,
        job_id: str,
        request: TargetCancelRequest,
    ) -> TargetJobStatus: ...


class HttpObserverPort:
    """Explicit configuration-backed observer registry using the v1 HTTP client."""

    def __init__(self, registrations: dict[str, ContentObserverClient]) -> None:
        self._registrations = dict(registrations)

    def descriptor(self, registration_id: str) -> ObserverDescriptor:
        return self._client(registration_id).descriptor()

    def observe(
        self,
        registration_id: str,
        invocation: ObservationInvocation,
    ) -> ObservationResult:
        return self._client(registration_id).observe(invocation)

    def _client(self, registration_id: str) -> ContentObserverClient:
        try:
            return self._registrations[registration_id]
        except KeyError as exc:
            raise KeyError(f"unknown content-observer registration: {registration_id}") from exc


class HttpTargetPort:
    """Explicit configuration-backed target registry using the v1 HTTP client."""

    def __init__(self, registrations: dict[str, TargetClient]) -> None:
        self._registrations = dict(registrations)

    def contract(self, registration_id: str) -> TargetContract:
        return self._client(registration_id).contract()

    def preflight(
        self,
        registration_id: str,
        request: TargetPreflightRequest,
    ) -> TargetPreflightResponse:
        return self._client(registration_id).preflight(request)

    def put_job(
        self,
        registration_id: str,
        request: TargetJobRequest,
    ) -> TargetJobStatus:
        return self._client(registration_id).put_job(request)

    def get_job(self, registration_id: str, job_id: str) -> TargetJobStatus:
        return self._client(registration_id).status(job_id)

    def cancel_job(
        self,
        registration_id: str,
        job_id: str,
        request: TargetCancelRequest,
    ) -> TargetJobStatus:
        return self._client(registration_id).cancel(job_id, request)

    def _client(self, registration_id: str) -> TargetClient:
        try:
            return self._registrations[registration_id]
        except KeyError as exc:
            raise KeyError(f"unknown target registration: {registration_id}") from exc


class Stove0Coordinator:
    """Advance one work record by one externally visible state transition."""

    def __init__(
        self,
        work: Stove0WorkService,
        *,
        riverhog: RiverhogControlPort,
        planning: PlanningPort,
        observers: ObserverPort,
        targets: TargetPort,
    ) -> None:
        self.work = work
        self.riverhog = riverhog
        self.planning = planning
        self.observers = observers
        self.targets = targets

    def create_or_resume(
        self,
        identity: WorkIdentity,
        *,
        preview: WorkflowPreview | None = None,
    ) -> WorkRecord:
        return self.work.create_or_resume(identity, preview=preview)

    def inspect_coordination(self, work_id: str) -> BranchSetEvaluation:
        record = self.work.store.load(work_id)
        if record is None:
            raise KeyError(work_id)
        return project_coordination(record, self.work.store).evaluation

    def step(self, work_id: str) -> WorkRecord:
        record = self.work.store.load(work_id)
        if record is None:
            raise KeyError(work_id)
        phase = record.phase
        if phase == "abandon_pending":
            self.riverhog.abandon_claim(record)
            return self.work.complete_abandon(
                work_id,
                expected_revision=record.revision,
            )
        if phase in {
            "claimed",
            "observing",
            "planning",
            "target_preflight",
            "queued",
            "executing",
            "output_finalizing",
            "coordinating",
        }:
            assert record.claim is not None
            renewed = self.riverhog.renew_claim(record.work, record.claim)
            if renewed != record.claim:
                return self.work.rebind_claim(
                    work_id,
                    claim_id=renewed.claim_id,
                    fence=renewed.fence,
                    expected_revision=record.revision,
                )
        if phase == "eligible":
            claim = self.riverhog.acquire_claim(record.work)
            return self.work.bind_claim(
                work_id,
                claim_id=claim.claim_id,
                fence=claim.fence,
                expected_revision=record.revision,
            )
        if phase == "claimed":
            if record.workflow_plan is not None:
                return self.work.activate_preplanned(
                    work_id,
                    expected_revision=record.revision,
                )
            assert record.claim is not None
            requests = self.planning.observation_requests(record.work)
            if requests:
                return self.work.begin_observations(
                    work_id,
                    requests,
                    expected_revision=record.revision,
                )
            return self.work.begin_planning(work_id, expected_revision=record.revision)
        if phase == "observing":
            return self._observe_one(record)
        if phase == "planning":
            evidence = tuple(
                ObservationEvidence(request=request, result=result)
                for request, result in zip(
                    record.observation_requests,
                    record.observation_results,
                    strict=True,
                )
            )
            decision = self.planning.workflow_plan(record.work, evidence)
            if isinstance(decision, WorkInapplicable):
                return self.work.mark_inapplicable(
                    work_id,
                    decision,
                    expected_revision=record.revision,
                )
            acceptance = record.preview_acceptance
            if (
                acceptance is not None
                and decision.plan.branch_set_sha256 != acceptance.branch_set_sha256
            ):
                return self.work.fail(
                    work_id,
                    WorkFailure(
                        code="accepted-preview-changed",
                        message=(
                            "Current observation and routing authorities no longer produce "
                            "the accepted workflow preview."
                        ),
                        retryable=True,
                    ),
                    expected_revision=record.revision,
                )
            return self.work.admit_branch_set(
                work_id,
                decision,
                expected_revision=record.revision,
            )
        if phase == "coordinating":
            if record.coordination_cancel_requested:
                return self._advance_coordination_cancel(record)
            projection = project_coordination(record, self.work.store)
            if projection.pending_join is not None:
                return self.work.admit_join(
                    work_id,
                    projection.pending_join,
                    projection.pending_join_selections,
                    expected_revision=record.revision,
                )
            if projection.evaluation.branch_set_succeeded:
                if not self._successful_children_complete(record):
                    return record
                if (
                    projection.evaluation.succeeded_branches
                    or projection.evaluation.join_settlement is not None
                ):
                    self.riverhog.settle_outcomes(record, projection.evaluation)
                return self._begin_or_complete_retirement(record)
            return self._converge_coordination_outcome(record, projection.evaluation)
        if phase == "target_preflight":
            return self._preflight(record)
        if phase == "queued":
            return self._queue_or_poll(record)
        if phase in {"executing", "output_finalizing"}:
            return self._poll_target(record)
        if phase == "verifying":
            output = self.riverhog.verify_and_settle(
                record,
                self._parent_outcome(record),
            )
            return self.work.verify_output(
                work_id,
                output,
                expected_revision=record.revision,
            )
        if phase == "settled":
            return self._begin_or_complete_retirement(record)
        if phase == "retirement_pending":
            return self._retire_one(record)
        return record

    def retry(self, work_id: str) -> WorkRecord:
        """Restart one retryable failure under a fresh Riverhog fence."""

        record = self.work.store.load(work_id)
        if record is None:
            raise KeyError(work_id)
        if record.phase != "failed" or record.failure is None:
            raise RuntimeError("only failed stove0 work can be retried")
        if not record.failure.retryable or record.claim is None:
            raise RuntimeError("stove0 work failure is terminal")
        if record.branch_set_plan is not None:
            return self._retry_coordination(record)
        restarted = self.riverhog.restart_claim(record.work, record.claim)
        return self.work.retry_failed(
            work_id,
            claim_id=restarted.claim_id,
            fence=restarted.fence,
            expected_revision=record.revision,
        )

    def cancel(self, work_id: str, *, reason: str | None = None) -> WorkRecord:
        """Request cancellation without creating another workflow authority.

        Work that has not reached a target enters a durable claim-abandonment
        phase. Active target work uses the target's explicit cancellation
        contract first; repeated calls remain idempotent through the accepted job
        identity. Riverhog revokes scoped capabilities before stove0 records the
        final canceled state.
        """

        record = self.work.store.load(work_id)
        if record is None:
            raise KeyError(work_id)
        if record.phase in {"complete", "inapplicable", "canceled"}:
            return record
        if record.phase == "failed":
            if record.failure is None or not record.failure.retryable:
                return record
            return self.work.cancel(work_id, expected_revision=record.revision)
        if record.phase in {"settled", "retirement_pending"}:
            raise RuntimeError("settled work cannot be canceled")
        if record.phase == "coordinating" and record.branch_set_plan is not None:
            return self.work.request_coordination_cancel(
                work_id,
                expected_revision=record.revision,
            )
        if record.target_request is None or record.workflow_plan is None:
            return self.work.cancel(work_id, expected_revision=record.revision)
        status = self.targets.cancel_job(
            record.workflow_plan.target_registration_id,
            record.target_request.declaration.job_id,
            TargetCancelRequest(reason=reason),
        )
        operation = self.planning.operation_contract(record.workflow_plan.operation)
        return self.work.record_target_status(
            work_id,
            status,
            operation=operation,
            expected_revision=record.revision,
        )

    def _observe_one(self, record: WorkRecord) -> WorkRecord:
        assert record.claim is not None
        completed = {item.request_id for item in record.observation_results}
        request = next(
            (item for item in record.observation_requests if item.request_id not in completed),
            None,
        )
        if request is None:
            raise RuntimeError("observing work has no pending observation request")
        descriptor = self.observers.descriptor(request.observer_registration_id)
        if descriptor.descriptor_sha256 != request.observer_descriptor_sha256:
            raise RuntimeError("configured observer descriptor changed after request sealing")
        authority = self.riverhog.observation_authority(record.claim, request)
        result = self.observers.observe(
            request.observer_registration_id,
            ObservationInvocation(
                request=request,
                claim_id=record.claim.claim_id,
                fence=record.claim.fence,
                runtime=authority,
            ),
        )
        return self.work.record_observation(
            record.work_id,
            result,
            descriptor=descriptor,
            expected_revision=record.revision,
        )

    def _preflight(self, record: WorkRecord) -> WorkRecord:
        plan = record.workflow_plan
        if plan is None:
            raise RuntimeError("target preflight work has no workflow plan")
        target = self.targets.contract(plan.target_registration_id)
        if target.contract_sha256 != plan.target_contract_sha256:
            raise RuntimeError("configured target contract changed after workflow planning")
        request = self.planning.target_preflight_request(
            plan,
            self._selection_documents(record),
        )
        response = self.targets.preflight(plan.target_registration_id, request)
        validate_preflight_response_against_request(response, request)
        if (
            record.expected_target_plan_sha256 is not None
            and response.plan.plan_sha256 != record.expected_target_plan_sha256
        ):
            return self.work.fail(
                record.work_id,
                WorkFailure(
                    code="accepted-preview-target-changed",
                    message=(
                        "Current target preflight no longer produces the plan accepted "
                        "by the workflow preview."
                    ),
                    retryable=True,
                ),
                expected_revision=record.revision,
            )
        return self.work.seal_target_plan(
            record.work_id,
            target=target,
            plan=response.plan,
            expected_revision=record.revision,
        )

    def _queue_or_poll(self, record: WorkRecord) -> WorkRecord:
        if record.target_request is not None:
            return self._poll_target(record)
        if (
            record.claim is None
            or record.workflow_plan is None
            or record.target_plan is None
            or record.controller_evidence is None
        ):
            raise RuntimeError("queued work is missing its sealed authorities")
        self.riverhog.seal_execution(
            record.claim,
            record.controller_evidence,
            record.workflow_plan,
            record.target_plan,
        )
        authority = self.riverhog.target_authority(
            record.claim,
            record.controller_evidence,
            record.target_plan,
        )
        declaration = TargetJobDeclaration(
            job_id=(record.controller_evidence.execution_envelope.execution_envelope_sha256),
            claim_id=record.claim.claim_id,
            fence=record.claim.fence,
            controller_evidence=record.controller_evidence,
            plan=record.target_plan,
            workspace_assurance=authority.workspace_assurance,
        )
        invocation = TargetJobRequest.seal(declaration, authority.runtime)
        accepted = self.work.bind_target_request(
            record.work_id,
            invocation,
            expected_revision=record.revision,
        )
        status = self.targets.put_job(
            record.workflow_plan.target_registration_id,
            invocation,
        )
        operation = self.planning.operation_contract(record.workflow_plan.operation)
        return self.work.record_target_status(
            record.work_id,
            status,
            operation=operation,
            expected_revision=accepted.revision,
        )

    def _poll_target(self, record: WorkRecord) -> WorkRecord:
        if (
            record.claim is None
            or record.workflow_plan is None
            or record.target_request is None
            or record.controller_evidence is None
            or record.target_plan is None
        ):
            raise RuntimeError("active target work has no accepted target authorities")
        authority = self.riverhog.target_authority(
            record.claim,
            record.controller_evidence,
            record.target_plan,
        )
        refreshed = TargetJobRequest(
            declaration=record.target_request.declaration,
            runtime=authority.runtime,
            request_sha256=record.target_request.request_sha256,
        )
        status = self.targets.put_job(
            record.workflow_plan.target_registration_id,
            refreshed,
        )
        operation = self.planning.operation_contract(record.workflow_plan.operation)
        return self.work.record_target_status(
            record.work_id,
            status,
            operation=operation,
            expected_revision=record.revision,
        )

    def _begin_or_complete_retirement(self, record: WorkRecord) -> WorkRecord:
        if record.branch_set_plan is not None:
            policy = record.branch_set_plan.retirement_policy
        elif record.workflow_plan is not None:
            policy = record.workflow_plan.retirement_policy
        else:
            raise RuntimeError("settled work has no workflow or branch-set plan")
        if policy == "retain":
            self.riverhog.release_claim(record)
            return self.work.begin_retirement(
                record.work_id,
                (),
                expected_revision=record.revision,
            )
        if record.branch_set_plan is not None:
            operations = tuple(
                self.planning.operation_contract(branch.workflow_plan.operation)
                for branch in record.branch_set_plan.branches
            )
            if not all(operation.source_retirement_permitted for operation in operations):
                raise RuntimeError(
                    "every branch operation contract must authorize source retirement"
                )
        else:
            assert record.workflow_plan is not None
            operation = self.planning.operation_contract(record.workflow_plan.operation)
            if not operation.source_retirement_permitted:
                raise RuntimeError("operation contract does not authorize source retirement")
        if not self.riverhog.begin_retirement(record):
            return record
        return self.work.begin_retirement(
            record.work_id,
            tuple(item.collection_id for item in record.work.inputs),
            expected_revision=record.revision,
        )

    def _retire_one(self, record: WorkRecord) -> WorkRecord:
        if not record.retirement_remaining:
            raise RuntimeError("retirement phase has no remaining collection")
        collection_id = record.retirement_remaining[0]
        if not self.riverhog.retire_input(record, collection_id):
            return record
        if len(record.retirement_remaining) == 1:
            self.riverhog.release_claim(record)
        return self.work.record_retired(
            record.work_id,
            collection_id,
            expected_revision=record.revision,
        )

    def _selection_documents(
        self,
        record: WorkRecord,
    ) -> dict[str, ArtifactSelection]:
        binding = record.work.fork_join
        digests: tuple[str, ...]
        if isinstance(binding, BranchWorkBinding):
            digests = (binding.artifact_selection_sha256,)
        elif isinstance(binding, JoinWorkBinding):
            digests = tuple(item.artifact_selection_sha256 for item in binding.members)
        else:
            raise RuntimeError("target work has no exact branch or join artifact selection")
        documents: dict[str, ArtifactSelection] = {}
        for digest in digests:
            selection = self.work.store.load_selection(digest)
            if selection is None:
                raise RuntimeError(f"durable target selection is unavailable: {digest}")
            documents[digest] = selection
        return documents

    def _parent_outcome(
        self,
        record: WorkRecord,
    ) -> ParentOutcomeBinding | None:
        binding = record.work.fork_join
        parent_work_id: str
        outcome_id: str
        if isinstance(binding, BranchWorkBinding):
            parent_work_id = binding.parent_work_id
            outcome_id = f"branch/{binding.branch_id}"
        elif isinstance(binding, JoinWorkBinding):
            parent_work_id = binding.parent_work_id
            outcome_id = "join"
        else:
            return None
        parent = self.work.store.load(parent_work_id)
        if (
            parent is None
            or parent.phase != "coordinating"
            or parent.claim is None
            or parent.branch_set_plan is None
        ):
            raise RuntimeError("coordination parent claim is unavailable")
        return ParentOutcomeBinding(
            claim=parent.claim,
            outcome_id=outcome_id,
        )

    def _advance_coordination_cancel(self, record: WorkRecord) -> WorkRecord:
        if record.branch_set_plan is None:
            raise RuntimeError("coordination cancellation has no branch-set plan")
        terminal = {"complete", "inapplicable", "failed", "canceled"}
        for child_id in self._coordination_child_ids(record):
            child = self.work.store.load(child_id)
            if child is None:
                raise RuntimeError("coordination cancellation child is unavailable")
            if child.phase not in terminal:
                self.cancel(child_id, reason="parent coordination canceled")
                return record
        return self.work.cancel(record.work_id, expected_revision=record.revision)

    def _successful_children_complete(self, record: WorkRecord) -> bool:
        if record.branch_set_plan is None:
            raise RuntimeError("successful coordination has no branch-set plan")
        for child_id in self._coordination_child_ids(record):
            child = self.work.store.load(child_id)
            if child is None:
                raise RuntimeError("successful coordination child is unavailable")
            if child.phase != "complete":
                return False
        return True

    def _converge_coordination_outcome(
        self,
        record: WorkRecord,
        evaluation: BranchSetEvaluation,
    ) -> WorkRecord:
        children = self._coordination_children(record)
        terminal = {"complete", "inapplicable", "failed", "canceled"}
        if any(child.phase not in terminal for child in children):
            return record
        failed = tuple(child for child in children if child.phase == "failed")
        if failed:
            failures = tuple(child.failure for child in failed)
            if any(failure is None for failure in failures):
                raise RuntimeError("failed coordination child has no failure details")
            retryable = all(failure is not None and failure.retryable for failure in failures)
            return self.work.fail(
                record.work_id,
                WorkFailure(
                    code="branch-set-failed",
                    message=(
                        "Required branch/join work failed: "
                        + ", ".join(child.work_id for child in failed)
                    )[:1000],
                    retryable=retryable,
                ),
                expected_revision=record.revision,
            )
        if evaluation.inapplicable_branch_ids or evaluation.join_state == "inapplicable":
            return self.work.mark_inapplicable(
                record.work_id,
                WorkInapplicable(
                    code="branch-set-inapplicable",
                    message="Required branch/join work is inapplicable.",
                ),
                expected_revision=record.revision,
            )
        if evaluation.canceled_branch_ids or evaluation.join_state == "canceled":
            return self.work.cancel(record.work_id, expected_revision=record.revision)
        return record

    def _retry_coordination(self, record: WorkRecord) -> WorkRecord:
        assert record.branch_set_plan is not None
        assert record.claim is not None
        for child_id in self._coordination_child_ids(record):
            child = self.work.store.load(child_id)
            if child is None:
                raise RuntimeError("coordination retry child is unavailable")
            if child.phase == "failed":
                if child.failure is None or not child.failure.retryable:
                    raise RuntimeError("coordination contains a terminal child failure")
                self.retry(child_id)
            elif child.phase in {"inapplicable", "canceled"}:
                raise RuntimeError("coordination contains a non-retryable child outcome")
        restarted = self.riverhog.restart_claim(record.work, record.claim)
        return self.work.retry_coordination(
            record.work_id,
            claim_id=restarted.claim_id,
            fence=restarted.fence,
            expected_revision=record.revision,
        )

    def _coordination_child_ids(self, record: WorkRecord) -> tuple[str, ...]:
        if record.branch_set_plan is None:
            raise RuntimeError("coordination has no branch-set plan")
        child_ids = [
            branch.workflow_plan.work.work_id for branch in record.branch_set_plan.branches
        ]
        if record.join_plan is not None:
            child_ids.append(record.join_plan.work.work_id)
        return tuple(child_ids)

    def _coordination_children(self, record: WorkRecord) -> tuple[WorkRecord, ...]:
        children: list[WorkRecord] = []
        for child_id in self._coordination_child_ids(record):
            child = self.work.store.load(child_id)
            if child is None:
                raise RuntimeError("coordination child is unavailable")
            children.append(child)
        return tuple(children)


__all__ = [
    "HttpObserverPort",
    "HttpTargetPort",
    "ParentOutcomeBinding",
    "ObserverPort",
    "PlanningPort",
    "RiverhogControlPort",
    "Stove0Coordinator",
    "TargetInvocationAuthority",
    "TargetPort",
]

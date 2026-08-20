"""Single authoritative stove0 work-state kernel.

The kernel contains no file-format logic, target implementation, HTTP transport,
or payload storage. Controller and worker processes mutate one record through a
compare-and-swap store; observers and targets remain subordinate execution ports.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import Any, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from stove0_protocol import (
    ControllerEvidence,
    ControllerEvidencePayload,
    ExecutionEnvelope,
    ExecutionEnvelopePayload,
    ObservationRequest,
    ObservationResult,
    ObserverDescriptor,
    TargetPlanBinding,
    WorkflowPlan,
    WorkIdentity,
    validate_observation_result,
)
from stove0_target_support import (
    AcceptedTargetJob,
    OperationContract,
    OutputCollectionRef,
    TargetContract,
    TargetJobRequest,
    TargetJobStatus,
    TransformPlan,
    validate_status_against_request,
)

WorkPhase = Literal[
    "eligible",
    "claimed",
    "observing",
    "planning",
    "target_preflight",
    "queued",
    "executing",
    "output_finalizing",
    "verifying",
    "settled",
    "retirement_pending",
    "abandon_pending",
    "complete",
    "inapplicable",
    "failed",
    "canceled",
]
TerminalPhase = Literal["complete", "inapplicable", "failed", "canceled"]
AbandonOutcome = Literal["inapplicable", "failed", "canceled"]


class Stove0StateError(RuntimeError):
    pass


class ConcurrentWorkUpdate(Stove0StateError):
    pass


class Stove0StateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClaimBinding(Stove0StateModel):
    claim_id: str = Field(min_length=1, max_length=160)
    fence: int = Field(ge=1)


class WorkFailure(Stove0StateModel):
    code: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=1000)
    retryable: bool


class WorkInapplicable(Stove0StateModel):
    code: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=1000)


class WorkRecord(Stove0StateModel):
    format: Literal["stove0-work-record/v1"] = "stove0-work-record/v1"
    work: WorkIdentity
    phase: WorkPhase = "eligible"
    revision: int = Field(default=1, ge=1)
    claim: ClaimBinding | None = None
    observation_requests: tuple[ObservationRequest, ...] = ()
    observation_results: tuple[ObservationResult, ...] = ()
    workflow_plan: WorkflowPlan | None = None
    target_plan: TransformPlan | None = None
    controller_evidence: ControllerEvidence | None = None
    target_request: AcceptedTargetJob | None = None
    target_status: TargetJobStatus | None = None
    output: OutputCollectionRef | None = None
    retirement_remaining: tuple[int, ...] = ()
    failure: WorkFailure | None = None
    inapplicable: WorkInapplicable | None = None
    abandon_outcome: AbandonOutcome | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if self.phase == "eligible" and self.claim is not None:
            raise ValueError("eligible work cannot already hold a claim")
        inactive_without_claim = {"eligible", "failed", "canceled", "inapplicable"}
        if self.phase not in inactive_without_claim and self.claim is None:
            raise ValueError("active work phases require a claim")
        if self.output is not None and self.phase not in {
            "verifying",
            "settled",
            "retirement_pending",
            "abandon_pending",
            "complete",
        }:
            raise ValueError("output identity appears before verification")
        if (
            self.phase
            in {
                "abandon_pending",
                "failed",
                "canceled",
                "inapplicable",
            }
            and self.output is not None
        ):
            raise ValueError("non-success terminal work cannot contain an output")
        failure_phases = {"failed"}
        if self.phase == "abandon_pending" and self.abandon_outcome == "failed":
            failure_phases.add("abandon_pending")
        if self.phase in failure_phases and self.failure is None:
            raise ValueError("failed work requires failure details")
        if self.phase not in failure_phases and self.failure is not None:
            raise ValueError("only failed work may retain failure details")
        inapplicable_phases = {"inapplicable"}
        if self.phase == "abandon_pending" and self.abandon_outcome == "inapplicable":
            inapplicable_phases.add("abandon_pending")
        if self.phase in inapplicable_phases and self.inapplicable is None:
            raise ValueError("inapplicable work requires a terminal outcome")
        if self.phase not in inapplicable_phases and self.inapplicable is not None:
            raise ValueError("only inapplicable work may retain that outcome")
        if self.phase == "abandon_pending" and self.abandon_outcome is None:
            raise ValueError("abandon_pending work requires a terminal outcome")
        if self.phase != "abandon_pending" and self.abandon_outcome is not None:
            raise ValueError("only abandon_pending work may retain an abandon outcome")
        if self.retirement_remaining and self.phase != "retirement_pending":
            raise ValueError("retirement work must remain in retirement_pending")
        return self

    @property
    def work_id(self) -> str:
        return self.work.work_id


class WorkStore(Protocol):
    def load(self, work_id: str) -> WorkRecord | None: ...

    def create(self, record: WorkRecord) -> WorkRecord: ...

    def compare_and_swap(
        self,
        work_id: str,
        *,
        expected_revision: int,
        replacement: WorkRecord,
    ) -> WorkRecord: ...


class InMemoryWorkStore:
    """Thread-safe deterministic store used by tests and in-process prototypes."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[str, WorkRecord] = {}

    def load(self, work_id: str) -> WorkRecord | None:
        with self._lock:
            return self._records.get(work_id)

    def create(self, record: WorkRecord) -> WorkRecord:
        with self._lock:
            existing = self._records.get(record.work_id)
            if existing is not None:
                if existing.work != record.work:
                    raise ConcurrentWorkUpdate("work identity was reused with another payload")
                return existing
            self._records[record.work_id] = record
            return record

    def compare_and_swap(
        self,
        work_id: str,
        *,
        expected_revision: int,
        replacement: WorkRecord,
    ) -> WorkRecord:
        with self._lock:
            current = self._records.get(work_id)
            if current is None:
                raise KeyError(work_id)
            if current.revision != expected_revision:
                raise ConcurrentWorkUpdate(
                    f"stale stove0 work revision: {expected_revision} != {current.revision}"
                )
            if replacement.work_id != work_id or replacement.revision != expected_revision + 1:
                raise ValueError("replacement work record has an invalid identity or revision")
            self._records[work_id] = replacement
            return replacement


class Stove0WorkService:
    """Validated state transitions for one collection transformation authority."""

    def __init__(self, store: WorkStore) -> None:
        self.store = store

    def create_or_resume(self, work: WorkIdentity) -> WorkRecord:
        return self.store.create(WorkRecord(work=work))

    def bind_claim(
        self,
        work_id: str,
        *,
        claim_id: str,
        fence: int,
        expected_revision: int,
    ) -> WorkRecord:
        record = self._load(work_id, expected_revision)
        if record.phase not in {"eligible", "claimed"}:
            raise Stove0StateError(f"work cannot bind a claim from {record.phase}")
        binding = ClaimBinding(claim_id=claim_id, fence=fence)
        if record.claim is not None and record.claim != binding:
            raise Stove0StateError("work is already bound to another claim generation")
        return self._replace(record, phase="claimed", claim=binding)

    def rebind_claim(
        self,
        work_id: str,
        *,
        claim_id: str,
        fence: int,
        expected_revision: int,
    ) -> WorkRecord:
        """Reset unsettled work under a newer Riverhog fencing generation.

        Any observer or target execution authorized by the prior generation is
        deliberately discarded. The immutable work identity is retained, while
        planning and execution are repeated to obtain a new fence-bound output
        intent. A finalized output from the stale generation can never be settled
        through this record.
        """

        record = self._load(work_id, expected_revision)
        if record.claim is None or record.phase in {
            "eligible",
            "settled",
            "retirement_pending",
            "abandon_pending",
            "complete",
            "inapplicable",
            "failed",
            "canceled",
        }:
            raise Stove0StateError(f"work cannot rebind a claim from {record.phase}")
        replacement = ClaimBinding(claim_id=claim_id, fence=fence)
        if replacement.claim_id != record.claim.claim_id:
            raise Stove0StateError("Riverhog returned another processing claim identity")
        if replacement.fence <= record.claim.fence:
            raise Stove0StateError("replacement claim fence must advance")
        return self._replace(
            record,
            phase="claimed",
            claim=replacement,
            observation_requests=(),
            observation_results=(),
            workflow_plan=None,
            target_plan=None,
            controller_evidence=None,
            target_request=None,
            target_status=None,
            output=None,
            retirement_remaining=(),
            failure=None,
            inapplicable=None,
            abandon_outcome=None,
        )

    def begin_planning(
        self,
        work_id: str,
        *,
        expected_revision: int,
    ) -> WorkRecord:
        """Advance work that requires no content observation."""

        record = self._load(work_id, expected_revision)
        if record.phase not in {"claimed", "planning"}:
            raise Stove0StateError(f"work cannot begin planning from {record.phase}")
        if record.observation_requests or record.observation_results:
            raise Stove0StateError("observed work must complete its observation phase")
        return self._replace(record, phase="planning")

    def begin_observations(
        self,
        work_id: str,
        requests: Sequence[ObservationRequest],
        *,
        expected_revision: int,
    ) -> WorkRecord:
        record = self._load(work_id, expected_revision)
        if record.phase not in {"claimed", "observing"} or record.claim is None:
            raise Stove0StateError(f"work cannot begin observations from {record.phase}")
        normalized = tuple(sorted(requests, key=lambda item: item.request_id))
        if not normalized:
            raise ValueError("observation phase requires at least one request")
        if len({item.request_id for item in normalized}) != len(normalized):
            raise ValueError("observation requests must be unique")
        roots = {
            (item.collection_id, item.manifest_sha256, item.content_etag)
            for item in record.work.inputs
        }
        for request in normalized:
            if request.work_id != record.work_id:
                raise ValueError("observation request does not bind the current work")
            if any(
                (
                    subject.collection.collection_id,
                    subject.collection.manifest_sha256,
                    subject.collection.content_etag,
                )
                not in roots
                for subject in request.subjects
            ):
                raise ValueError("observation request references an input outside the work")
        if record.observation_requests and record.observation_requests != normalized:
            raise Stove0StateError("observation request set is already sealed")
        return self._replace(
            record,
            phase="observing",
            observation_requests=normalized,
        )

    def record_observation(
        self,
        work_id: str,
        result: ObservationResult,
        *,
        descriptor: ObserverDescriptor,
        expected_revision: int,
    ) -> WorkRecord:
        record = self._load(work_id, expected_revision)
        if record.phase != "observing":
            raise Stove0StateError(f"work cannot record observations from {record.phase}")
        requests = {item.request_id: item for item in record.observation_requests}
        request = requests.get(result.request_id)
        if request is None:
            raise ValueError("observation result was not requested by this work")
        validate_observation_result(result, request, descriptor)
        results = {item.request_id: item for item in record.observation_results}
        existing = results.get(result.request_id)
        if existing is not None and existing != result:
            raise Stove0StateError("observation result identity changed")
        results[result.request_id] = result
        normalized = tuple(results[key] for key in sorted(results))
        phase: WorkPhase = "planning" if set(results) == set(requests) else "observing"
        return self._replace(record, phase=phase, observation_results=normalized)

    def seal_workflow_plan(
        self,
        work_id: str,
        plan: WorkflowPlan,
        *,
        expected_revision: int,
    ) -> WorkRecord:
        record = self._load(work_id, expected_revision)
        if record.phase not in {"planning", "target_preflight"}:
            raise Stove0StateError(f"work cannot seal a workflow plan from {record.phase}")
        requests = tuple(item.request for item in plan.observations)
        results = tuple(item.result for item in plan.observations)
        if (
            plan.work != record.work
            or requests != record.observation_requests
            or results != record.observation_results
        ):
            raise ValueError("workflow plan differs from the work or accepted observations")
        if record.workflow_plan is not None and record.workflow_plan != plan:
            raise Stove0StateError("workflow plan is already sealed")
        return self._replace(record, phase="target_preflight", workflow_plan=plan)

    def seal_target_plan(
        self,
        work_id: str,
        *,
        target: TargetContract,
        plan: TransformPlan,
        expected_revision: int,
    ) -> WorkRecord:
        record = self._load(work_id, expected_revision)
        workflow = record.workflow_plan
        if record.phase not in {"target_preflight", "queued"} or workflow is None:
            raise Stove0StateError(f"work cannot seal a target plan from {record.phase}")
        if (
            target.contract_sha256 != workflow.target_contract_sha256
            or plan.target_contract_sha256 != target.contract_sha256
            or plan.target_implementation_id != target.implementation_id
            or plan.operation_id != workflow.operation.id
            or plan.operation_contract_sha256 != workflow.operation.sha256
        ):
            raise ValueError("target preflight does not match the stove0 workflow plan")
        binding = TargetPlanBinding(
            protocol=target.protocol,
            target_implementation_id=target.implementation_id,
            target_contract_sha256=target.contract_sha256,
            operation_contract_sha256=plan.operation_contract_sha256,
            plan=plan.binding_document(),
            plan_sha256=plan.plan_sha256,
        )
        if record.claim is None:
            raise Stove0StateError("target plan requires a live claim binding")
        envelope = ExecutionEnvelope.seal(
            ExecutionEnvelopePayload(
                claim_id=record.claim.claim_id,
                fence=record.claim.fence,
                workflow_plan=workflow,
                target_plan=binding,
            )
        )
        evidence = ControllerEvidence.seal(ControllerEvidencePayload(execution_envelope=envelope))
        if record.target_plan is not None and (
            record.target_plan != plan or record.controller_evidence != evidence
        ):
            raise Stove0StateError("target plan is already sealed")
        return self._replace(
            record,
            phase="queued",
            target_plan=plan,
            controller_evidence=evidence,
        )

    def bind_target_request(
        self,
        work_id: str,
        request: TargetJobRequest,
        *,
        expected_revision: int,
    ) -> WorkRecord:
        record = self._load(work_id, expected_revision)
        if record.phase not in {"queued", "executing"} or record.controller_evidence is None:
            raise Stove0StateError(f"work cannot bind a target request from {record.phase}")
        if (
            request.declaration.controller_evidence != record.controller_evidence
            or request.declaration.claim_id != record.claim.claim_id  # type: ignore[union-attr]
            or request.declaration.fence != record.claim.fence  # type: ignore[union-attr]
        ):
            raise ValueError("target request does not bind the current stove0 work")
        accepted = request.accepted()
        if record.target_request is not None and record.target_request != accepted:
            raise Stove0StateError("target request identity is already sealed")
        return self._replace(record, phase="queued", target_request=accepted)

    def record_target_status(
        self,
        work_id: str,
        status: TargetJobStatus,
        *,
        operation: OperationContract,
        expected_revision: int,
    ) -> WorkRecord:
        record = self._load(work_id, expected_revision)
        request = record.target_request
        if request is None or record.phase not in {
            "queued",
            "executing",
            "output_finalizing",
            "verifying",
        }:
            raise Stove0StateError(f"work cannot record target status from {record.phase}")
        validate_status_against_request(status, request, operation)
        if record.target_status is not None:
            prior = record.target_status
            if (
                prior.state in {"succeeded", "inapplicable", "failed", "canceled"}
                and prior != status
            ):
                raise Stove0StateError("terminal target status is immutable")
            if status.attempt < prior.attempt:
                raise Stove0StateError("target attempt generation moved backward")
        phase: WorkPhase
        output: OutputCollectionRef | None = None
        failure: WorkFailure | None = None
        if status.state in {"queued", "interrupted"}:
            phase = "queued"
        elif status.state in {"running", "canceling"}:
            phase = "executing"
        elif status.state == "succeeded":
            phase = "verifying"
            output = status.output_collection
        elif status.state == "inapplicable":
            assert status.inapplicable is not None
            phase = "abandon_pending"
        elif status.state == "failed":
            assert status.failure is not None
            failure = WorkFailure(
                code=status.failure.code,
                message=status.failure.message,
                retryable=status.failure.retryable,
            )
            phase = "failed" if status.failure.retryable else "abandon_pending"
        else:
            phase = "abandon_pending"
        return self._replace(
            record,
            phase=phase,
            target_status=status,
            output=output,
            failure=failure,
            abandon_outcome=(
                "inapplicable"
                if phase == "abandon_pending" and status.inapplicable is not None
                else "failed"
                if phase == "abandon_pending" and failure is not None
                else "canceled"
                if phase == "abandon_pending"
                else None
            ),
            inapplicable=(
                WorkInapplicable(
                    code=status.inapplicable.code,
                    message=status.inapplicable.message,
                )
                if status.inapplicable is not None
                else None
            ),
        )

    def verify_output(
        self,
        work_id: str,
        output: OutputCollectionRef,
        *,
        expected_revision: int,
    ) -> WorkRecord:
        record = self._load(work_id, expected_revision)
        if record.phase != "verifying" or record.target_status is None:
            raise Stove0StateError(f"work cannot verify output from {record.phase}")
        if record.target_status.output_collection != output or record.output != output:
            raise ValueError("Riverhog verification differs from the target output identity")
        return self._replace(record, phase="settled", output=output)

    def begin_retirement(
        self,
        work_id: str,
        collection_ids: Sequence[int],
        *,
        expected_revision: int,
    ) -> WorkRecord:
        record = self._load(work_id, expected_revision)
        if record.phase != "settled" or record.workflow_plan is None:
            raise Stove0StateError(f"work cannot begin retirement from {record.phase}")
        if record.workflow_plan.retirement_policy == "retain":
            if collection_ids:
                raise ValueError("retained work cannot retire input collections")
            return self._replace(record, phase="complete")
        expected = tuple(item.collection_id for item in record.work.inputs)
        normalized = tuple(sorted(set(int(item) for item in collection_ids)))
        if normalized != expected:
            raise ValueError("retirement must name every exact input collection")
        return self._replace(
            record,
            phase="retirement_pending",
            retirement_remaining=normalized,
        )

    def record_retired(
        self,
        work_id: str,
        collection_id: int,
        *,
        expected_revision: int,
    ) -> WorkRecord:
        record = self._load(work_id, expected_revision)
        if record.phase != "retirement_pending":
            raise Stove0StateError(f"work cannot record retirement from {record.phase}")
        remaining = tuple(
            item for item in record.retirement_remaining if item != int(collection_id)
        )
        if remaining == record.retirement_remaining:
            raise ValueError("collection is not pending retirement")
        return self._replace(
            record,
            phase="complete" if not remaining else "retirement_pending",
            retirement_remaining=remaining,
        )

    def retry_failed(
        self,
        work_id: str,
        *,
        claim_id: str,
        fence: int,
        expected_revision: int,
    ) -> WorkRecord:
        """Restart one retryable failure under a fresh or renewed claim fence."""

        record = self._load(work_id, expected_revision)
        if record.phase != "failed" or record.failure is None or not record.failure.retryable:
            raise Stove0StateError("only retryable failed work may be restarted")
        if record.claim is None:
            raise Stove0StateError("retryable failed work has no claim binding")
        replacement = ClaimBinding(claim_id=claim_id, fence=fence)
        if replacement.claim_id != record.claim.claim_id:
            raise Stove0StateError("retry must retain the same Riverhog claim identity")
        if replacement.fence <= record.claim.fence:
            raise Stove0StateError("retry must advance the Riverhog claim fence")
        return self._replace(
            record,
            phase="claimed",
            claim=replacement,
            observation_requests=(),
            observation_results=(),
            workflow_plan=None,
            target_plan=None,
            controller_evidence=None,
            target_request=None,
            target_status=None,
            output=None,
            retirement_remaining=(),
            failure=None,
            inapplicable=None,
            abandon_outcome=None,
        )

    def mark_inapplicable(
        self,
        work_id: str,
        outcome: WorkInapplicable,
        *,
        expected_revision: int,
    ) -> WorkRecord:
        record = self._load(work_id, expected_revision)
        if record.phase not in {"claimed", "observing", "planning", "target_preflight"}:
            raise Stove0StateError(f"work cannot become inapplicable from {record.phase}")
        return self._replace(
            record,
            phase="abandon_pending",
            inapplicable=outcome,
            abandon_outcome="inapplicable",
        )

    def fail(
        self,
        work_id: str,
        failure: WorkFailure,
        *,
        expected_revision: int,
    ) -> WorkRecord:
        record = self._load(work_id, expected_revision)
        if record.phase == "abandon_pending":
            return record
        if record.phase in {"complete", "inapplicable", "failed", "canceled", "settled"}:
            raise Stove0StateError(f"work cannot fail from {record.phase}")
        return self._replace(
            record,
            phase="failed" if failure.retryable else "abandon_pending",
            failure=failure,
            abandon_outcome=None if failure.retryable else "failed",
        )

    def cancel(
        self,
        work_id: str,
        *,
        expected_revision: int,
    ) -> WorkRecord:
        record = self._load(work_id, expected_revision)
        if record.phase == "abandon_pending":
            return record
        if record.phase == "failed":
            if record.failure is None or not record.failure.retryable:
                raise Stove0StateError("terminal failed work cannot be canceled")
            return self._replace(
                record,
                phase="abandon_pending",
                failure=None,
                abandon_outcome="canceled",
            )
        if record.phase in {"complete", "inapplicable", "canceled", "settled"}:
            raise Stove0StateError(f"work cannot cancel from {record.phase}")
        return self._replace(
            record,
            phase="abandon_pending",
            abandon_outcome="canceled",
        )

    def complete_abandon(
        self,
        work_id: str,
        *,
        expected_revision: int,
    ) -> WorkRecord:
        """Record Riverhog's idempotent terminal claim abandonment.

        The intermediate phase makes the cross-service transition crash-safe:
        replay after either durable write converges on the same terminal result.
        """

        record = self._load(work_id, expected_revision)
        if record.phase != "abandon_pending" or record.abandon_outcome is None:
            raise Stove0StateError(f"work cannot complete abandonment from {record.phase}")
        return self._replace(
            record,
            phase=record.abandon_outcome,
            abandon_outcome=None,
        )

    def _load(self, work_id: str, expected_revision: int) -> WorkRecord:
        record = self.store.load(work_id)
        if record is None:
            raise KeyError(work_id)
        if record.revision != expected_revision:
            raise ConcurrentWorkUpdate(
                f"stale stove0 work revision: {expected_revision} != {record.revision}"
            )
        return record

    def _replace(self, record: WorkRecord, **updates: Any) -> WorkRecord:
        replacement = record.model_copy(update={**updates, "revision": record.revision + 1})
        replacement = WorkRecord.model_validate(replacement.model_dump(mode="python"))
        return self.store.compare_and_swap(
            record.work_id,
            expected_revision=record.revision,
            replacement=replacement,
        )


__all__ = [
    "AbandonOutcome",
    "ClaimBinding",
    "ConcurrentWorkUpdate",
    "InMemoryWorkStore",
    "Stove0StateError",
    "Stove0WorkService",
    "TerminalPhase",
    "WorkFailure",
    "WorkInapplicable",
    "WorkPhase",
    "WorkRecord",
    "WorkStore",
]

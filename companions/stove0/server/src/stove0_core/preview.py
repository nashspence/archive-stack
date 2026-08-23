"""Deterministic workflow preview using the production planning authorities.

A preview may observe exact immutable inputs and invoke target preflight, but it
never seals an execution plan with Riverhog, receives output-write authority,
starts a target job, or publishes a collection. The same :class:`PlanningPort`
is used by preview and execution so the preview is not an approximate simulator.
"""

from __future__ import annotations

from typing import Protocol

from stove0_protocol import (
    BranchSetDecision,
    BranchTargetPreview,
    ObservationEvidence,
    ObservationInvocation,
    ObservationRequest,
    ObserverRuntimeAuthority,
    PreviewOutcome,
    TargetPlanBinding,
    WorkflowPreview,
    WorkflowPreviewPayload,
    WorkflowPreviewRequest,
    WorkflowPreviewRequestPayload,
    WorkIdentity,
    validate_observation_result,
)
from stove0_target_protocol import validate_preflight_response_against_request

from stove0_core.coordinator import (
    ObserverPort,
    PlanningObservationTerminal,
    PlanningPort,
    TargetPort,
)
from stove0_core.work_state import ClaimBinding, WorkInapplicable


class PreviewRiverhogPort(Protocol):
    """Read-only Riverhog authority for one workflow preview."""

    def acquire_preview_claim(self, request: WorkflowPreviewRequest) -> ClaimBinding: ...

    def observation_authority(
        self,
        claim: ClaimBinding,
        request: ObservationRequest,
    ) -> ObserverRuntimeAuthority: ...

    def abandon_preview_claim(
        self,
        request: WorkflowPreviewRequest,
        claim: ClaimBinding,
    ) -> None: ...


class WorkflowPreviewService:
    """Resolve observation, routing, and target preflight without execution."""

    def __init__(
        self,
        *,
        riverhog: PreviewRiverhogPort,
        planning: PlanningPort,
        observers: ObserverPort,
        targets: TargetPort,
    ) -> None:
        self.riverhog = riverhog
        self.planning = planning
        self.observers = observers
        self.targets = targets

    def preview(self, work: object) -> WorkflowPreview:
        from stove0_protocol import WorkIdentity

        identity = WorkIdentity.model_validate(work)
        request = WorkflowPreviewRequest.seal(WorkflowPreviewRequestPayload(work=identity))
        claim = self.riverhog.acquire_preview_claim(request)
        observations: list[ObservationEvidence] = []
        try:
            evidence = self._observe_work(identity, claim, observations)
            decision = self.planning.workflow_plan(
                identity,
                evidence,
                nested_observer=lambda child: self._observe_work(child, claim, observations),
            )
            if isinstance(decision, WorkInapplicable):
                return WorkflowPreview.seal(
                    WorkflowPreviewPayload(
                        preview_id=request.preview_id,
                        state="inapplicable",
                        work=identity,
                        observations=tuple(
                            sorted(observations, key=lambda item: item.request.request_id)
                        ),
                        outcome=PreviewOutcome(
                            code=decision.code,
                            message=decision.message,
                        ),
                    )
                )

            if not isinstance(decision, BranchSetDecision):
                raise RuntimeError("planning returned an unsupported workflow decision")
            documents = decision.selection_documents
            target_plans: list[BranchTargetPreview] = []
            for branch in decision.leaf_branches():
                workflow = branch.workflow_plan
                target = self.targets.contract(workflow.target_registration_id)
                if target.contract_sha256 != workflow.target_contract_sha256:
                    raise RuntimeError("configured target contract changed after workflow planning")
                preflight_request = self.planning.target_preflight_request(
                    workflow,
                    documents,
                )
                response = self.targets.preflight(
                    workflow.target_registration_id,
                    preflight_request,
                )
                validate_preflight_response_against_request(response, preflight_request)
                plan = response.plan
                target_plans.append(
                    BranchTargetPreview(
                        branch_id=branch.branch_id,
                        work_id=workflow.work.work_id,
                        workflow_plan_sha256=workflow.workflow_plan_sha256,
                        target_plan=TargetPlanBinding(
                            protocol=target.protocol,
                            target_implementation_id=target.implementation_id,
                            target_contract_sha256=target.contract_sha256,
                            operation_contract_sha256=plan.operation_contract_sha256,
                            plan=plan.binding_document(),
                            plan_sha256=plan.plan_sha256,
                        ),
                    )
                )
            return WorkflowPreview.seal(
                WorkflowPreviewPayload(
                    preview_id=request.preview_id,
                    state="ready",
                    work=identity,
                    observations=tuple(
                        sorted(observations, key=lambda item: item.request.request_id)
                    ),
                    branch_set_plan=decision.plan,
                    branch_sets=decision.branch_sets,
                    selections=decision.selections,
                    target_plans=tuple(target_plans),
                )
            )
        except PlanningObservationTerminal as outcome:
            return WorkflowPreview.seal(
                WorkflowPreviewPayload(
                    preview_id=request.preview_id,
                    state=outcome.state,
                    work=identity,
                    observations=tuple(
                        sorted(observations, key=lambda item: item.request.request_id)
                    ),
                    outcome=PreviewOutcome(
                        code=outcome.code,
                        message=outcome.message,
                        retryable=outcome.retryable,
                    ),
                )
            )
        except Exception as exc:
            return WorkflowPreview.seal(
                WorkflowPreviewPayload(
                    preview_id=request.preview_id,
                    state="failed",
                    work=identity,
                    observations=tuple(observations),
                    outcome=PreviewOutcome(
                        code="workflow-preview-failed",
                        message=f"{type(exc).__name__}: {exc}"[:1000],
                        retryable=True,
                    ),
                )
            )
        finally:
            self.riverhog.abandon_preview_claim(request, claim)

    def _observe_work(
        self,
        work: WorkIdentity,
        claim: ClaimBinding,
        all_observations: list[ObservationEvidence],
    ) -> tuple[ObservationEvidence, ...]:
        evidence: list[ObservationEvidence] = []
        for observation_request in self.planning.observation_requests(work):
            if observation_request.work_id != work.work_id:
                raise RuntimeError("preview observation request differs from the work identity")
            descriptor = self.observers.descriptor(observation_request.observer_registration_id)
            if descriptor.descriptor_sha256 != observation_request.observer_descriptor_sha256:
                raise RuntimeError(
                    "configured observer descriptor changed after preview request sealing"
                )
            authority = self.riverhog.observation_authority(claim, observation_request)
            result = self.observers.observe(
                observation_request.observer_registration_id,
                ObservationInvocation(
                    request=observation_request,
                    claim_id=claim.claim_id,
                    fence=claim.fence,
                    runtime=authority,
                ),
            )
            validate_observation_result(result, observation_request, descriptor)
            if result.state == "inapplicable":
                assert result.inapplicable is not None
                raise PlanningObservationTerminal(
                    state="inapplicable",
                    code=result.inapplicable.code,
                    message=result.inapplicable.message,
                )
            if result.state == "failed":
                assert result.failure is not None
                raise PlanningObservationTerminal(
                    state="failed",
                    code=result.failure.code,
                    message=result.failure.message,
                    retryable=result.failure.retryable,
                )
            if result.state == "canceled":
                raise PlanningObservationTerminal(
                    state="canceled",
                    code="observer-canceled",
                    message="The content observer canceled the preview request.",
                )
            item = ObservationEvidence(request=observation_request, result=result)
            evidence.append(item)
            all_observations.append(item)
        return tuple(sorted(evidence, key=lambda item: item.request.request_id))


__all__ = ["PreviewRiverhogPort", "WorkflowPreviewService"]

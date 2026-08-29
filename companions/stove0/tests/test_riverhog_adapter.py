from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest
from riverhog_protocol import Conflict, NotFound
from riverhog_protocol.collection_workflows import (
    ArtifactDisposition,
    CollectionDerivation,
    CollectionProcessingOutcomeIdentity,
    CollectionRootIdentity,
)
from riverhog_protocol.collection_workflows import (
    canonical_json_sha256 as riverhog_canonical_json_sha256,
)
from stove0_core import ClaimBinding, Stove0RiverhogClient, WorkRecord
from stove0_observer_protocol import ObservationRequest, ObservationRequestPayload
from stove0_protocol import (
    ArtifactSelection,
    ArtifactSubject,
    BranchPlan,
    BranchSetPlan,
    BranchSettlement,
    CollectionRootRef,
    ControllerEvidence,
    ControllerEvidencePayload,
    ExecutionEnvelope,
    ExecutionEnvelopePayload,
    OperationRef,
    RecipeRef,
    TargetPlanBinding,
    WorkflowPlan,
    WorkflowPlanIntent,
    WorkflowPlanPayload,
    WorkflowPreviewRequest,
    WorkflowPreviewRequestPayload,
    WorkIdentity,
    WorkPayload,
    evaluate_branch_set,
)
from stove0_target_support import (
    EffectPlan,
    EffectPlanPayload,
    InputArtifact,
    OutputArtifact,
    OutputCollectionRef,
    TargetExecutionEvidence,
    TargetJobStatus,
    TargetProgress,
    TransformPlan,
    TransformPlanPayload,
)


def _sha(character: str) -> str:
    return character * 64


def _authorities(
    retirement_policy: str = "retain",
) -> tuple[WorkIdentity, WorkflowPlan, TransformPlan, ControllerEvidence]:
    work = WorkIdentity.seal(
        WorkPayload(
            recipe=RecipeRef(id="fixture.recipe/v1", revision=1, sha256=_sha("1")),
            inputs=(
                CollectionRootRef(
                    collection_id=1,
                    archive_root_sha256=_sha("2"),
                    content_identity=_sha("3"),
                ),
            ),
        )
    )
    workflow = WorkflowPlan.seal(
        WorkflowPlanPayload(
            work=work,
            operation=OperationRef(id="fixture.copy/v1", sha256=_sha("4")),
            target_registration_id="fixture-target",
            target_contract_sha256=_sha("5"),
            output_tags=("fixture-output",),
            retirement_policy=retirement_policy,
        )
    )
    target_plan = TransformPlan.seal(
        TransformPlanPayload(
            target_implementation_id="fixture.target/v1",
            target_contract_sha256=_sha("5"),
            operation_id="fixture.copy/v1",
            operation_contract_sha256=_sha("4"),
            inputs=(
                InputArtifact(
                    id="source",
                    role="fixture.source/v1",
                    collection=work.inputs[0],
                    path="source/input.bin",
                    bytes=12,
                    sha256=_sha("e"),
                ),
            ),
            intent={},
        )
    )
    binding = TargetPlanBinding(
        protocol=target_plan.protocol,
        target_implementation_id="fixture.target/v1",
        target_contract_sha256=_sha("5"),
        operation_contract_sha256=_sha("4"),
        plan=target_plan.binding_document(),
        plan_sha256=target_plan.plan_sha256,
    )
    envelope = ExecutionEnvelope.seal(
        ExecutionEnvelopePayload(
            claim_id="claim-1",
            fence=1,
            workflow_plan=workflow,
            target_plan=binding,
        )
    )
    evidence = ControllerEvidence.seal(ControllerEvidencePayload(execution_envelope=envelope))
    return work, workflow, target_plan, evidence


def _effect_authorities() -> tuple[WorkIdentity, WorkflowPlan, EffectPlan, ControllerEvidence]:
    work, _workflow, _target_plan, _evidence = _authorities()
    workflow = WorkflowPlan.seal(
        WorkflowPlanPayload(
            work=work,
            result_kind="external-effect",
            operation=OperationRef(id="fixture.effect/v1", sha256=_sha("4")),
            target_registration_id="fixture-effect-target",
            target_contract_sha256=_sha("5"),
            retirement_policy="retain",
        )
    )
    target_plan = EffectPlan.seal(
        EffectPlanPayload(
            target_implementation_id="fixture.effect-target/v1",
            target_contract_sha256=_sha("5"),
            operation_id="fixture.effect/v1",
            operation_contract_sha256=_sha("4"),
            inputs=(
                InputArtifact(
                    id="source",
                    role="fixture.source/v1",
                    collection=work.inputs[0],
                    path="source/input.bin",
                    bytes=12,
                    sha256=_sha("e"),
                ),
            ),
            intent={},
        )
    )
    binding = TargetPlanBinding(
        protocol=target_plan.protocol,
        target_implementation_id=target_plan.target_implementation_id,
        target_contract_sha256=target_plan.target_contract_sha256,
        operation_contract_sha256=target_plan.operation_contract_sha256,
        plan=target_plan.binding_document(),
        plan_sha256=target_plan.plan_sha256,
    )
    envelope = ExecutionEnvelope.seal(
        ExecutionEnvelopePayload(
            claim_id="claim-1",
            fence=1,
            workflow_plan=workflow,
            target_plan=binding,
        )
    )
    evidence = ControllerEvidence.seal(ControllerEvidencePayload(execution_envelope=envelope))
    return work, workflow, target_plan, evidence


class FixtureApi:
    base_url = "https://riverhog.invalid"
    allow_insecure_http = False

    def __init__(self) -> None:
        self.execution_id: str | None = None
        self.derivation: CollectionDerivation | None = None
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fence = 1
        self.claim_state = "active"
        self.retirement_state = "retiring"
        self.deletion_blockers: list[str] = []
        self.expire_renewal = False
        self.deleted: set[int] = set()
        self.processing_outcomes: list[dict[str, object]] = []

    def create_or_resume_processing_claim(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("claim", kwargs))
        if self.expire_renewal:
            self.fence += 1
            self.expire_renewal = False
        self.deleted: set[int] = set()
        return {
            "id": "claim-1",
            "fence": self.fence,
            "work_id": kwargs["work_id"],
            "state": self.claim_state,
        }

    def renew_processing_claim(self, claim_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("renew", {"claim_id": claim_id, **kwargs}))
        if self.expire_renewal:
            raise Conflict("claim lease expired")
        return {"id": claim_id, "fence": kwargs["fence"]}

    def restart_processing_claim(self, claim_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("restart", {"claim_id": claim_id, **kwargs}))
        self.fence += 1
        self.execution_id = None
        return {"id": claim_id, "fence": self.fence, "state": "active"}

    def create_transform_capability(self, claim_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("capability", {"claim_id": claim_id, **kwargs}))
        actions = tuple(sorted(kwargs["actions"]))
        principal = (
            f"transform:{self.execution_id}"
            if "write-output" in actions
            else f"claim:{claim_id}"
            if str(kwargs["audience"]).startswith("stove0.target/")
            else f"observe:{claim_id}:{kwargs['fence']}"
        )
        return {
            "claim_id": claim_id,
            "fence": kwargs["fence"],
            "audience": kwargs["audience"],
            "actions": list(actions),
            "principal_app": principal,
            "token": f"secret-{len(self.calls)}",
        }

    def seal_processing_claim_plan(self, claim_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("seal", {"claim_id": claim_id, **kwargs}))
        self.execution_id = kwargs["execution_id"]
        return {
            "id": claim_id,
            "fence": kwargs["fence"],
            "plan": {"execution_id": self.execution_id},
        }

    def settle_processing_claim(self, claim_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("settle", {"claim_id": claim_id, **kwargs}))
        self.derivation = CollectionDerivation.from_mapping(kwargs["derivation"])
        return {
            "id": claim_id,
            "fence": kwargs["fence"],
            "state": "settled",
        }

    def get_processing_claim(self, claim_id: str) -> dict[str, Any]:
        self.calls.append(("get-claim", {"claim_id": claim_id}))
        return {
            "id": claim_id,
            "fence": self.fence,
            "state": self.claim_state,
            "outcomes": self.processing_outcomes,
        }

    def settle_processing_claim_outcomes(
        self,
        claim_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append(("settle-outcomes", {"claim_id": claim_id, **kwargs}))
        return {
            "id": claim_id,
            "fence": kwargs["fence"],
            "state": "settled",
        }

    def abandon_processing_claim(self, claim_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("abandon", {"claim_id": claim_id, **kwargs}))
        return {
            "id": claim_id,
            "fence": kwargs["fence"],
            "state": "abandoned",
        }

    def get_collection(self, collection_id: int) -> dict[str, Any]:
        assert self.derivation is not None
        return {
            "id": collection_id,
            "archive_root_sha256": _sha("7"),
            "content_identity": _sha("8"),
        }

    @contextmanager
    def stream_collection_tags(self, collection_id: int):  # type: ignore[no-untyped-def]
        assert self.derivation is not None
        self.calls.append(("stream-tags", {"collection_id": collection_id}))
        yield iter(
            {"collection_id": collection_id, "tag": tag} for tag in self.derivation.output_tags
        )

    def get_collection_derivation(self, collection_id: int) -> dict[str, Any]:
        assert self.derivation is not None
        return {
            "collection_id": collection_id,
            "document_sha256": self.derivation.sha256,
            "derivation": self.derivation.as_dict(),
        }

    def begin_processing_claim_retirement(self, claim_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("retirement", {"claim_id": claim_id, **kwargs}))
        return {"id": claim_id, "fence": kwargs["fence"], "state": self.retirement_state}

    def plan_collection_deletion(self, collection_id: int, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("deletion-plan", {"collection_id": collection_id, **kwargs}))
        if collection_id in self.deleted:
            raise NotFound("collection is already absent")
        return {
            "status": "blocked" if self.deletion_blockers else "ready",
            "blockers": self.deletion_blockers,
            "challenge": None if self.deletion_blockers else "delete-me",
        }

    def delete_collection(self, collection_id: int, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("delete", {"collection_id": collection_id, **kwargs}))
        self.deleted.add(collection_id)
        return {"status": "deleted"}

    def release_processing_claim(self, claim_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("release", {"claim_id": claim_id, **kwargs}))
        return {"id": claim_id, "fence": kwargs["fence"], "state": "released"}


def _verifying_record(
    work: WorkIdentity,
    workflow: WorkflowPlan,
    evidence: ControllerEvidence,
) -> WorkRecord:
    execution_id = evidence.execution_envelope.execution_envelope_sha256
    controller_document = evidence.model_dump(mode="json", by_alias=True, exclude_none=True)
    output = OutputArtifact(
        id="output",
        role="fixture.output/v1",
        path="output/result.bin",
        bytes=12,
        sha256=_sha("9"),
        derived_from=("source",),
    )
    derivation = CollectionDerivation(
        execution_id=execution_id,
        claim_id="claim-1",
        fence=1,
        recipe=work.recipe.to_identity(),
        operation=workflow.operation.to_identity(),
        inputs=work.root_identities(),
        output_tags=workflow.output_tags,
        execution_envelope_sha256=execution_id,
        execution_sha256=_sha("a"),
        controller_evidence=controller_document,
        controller_evidence_sha256=riverhog_canonical_json_sha256(controller_document),
        dispositions=(
            ArtifactDisposition(
                input_collection_id=1,
                input_archive_root_sha256=_sha("2"),
                input_path="source/input.bin",
                status="transformed",
                outputs=(output.path,),
            ),
        ),
    )
    output_ref = OutputCollectionRef(
        collection_id=7,
        archive_root_sha256=_sha("7"),
        content_identity=_sha("8"),
        derivation_sha256=derivation.sha256,
    )
    status = TargetJobStatus(
        job_id=execution_id,
        state="succeeded",
        attempt=1,
        request_sha256=_sha("b"),
        plan_sha256=evidence.execution_envelope.target_plan.plan_sha256,
        progress=TargetProgress(phase="done", completed=1, total=1),
        outputs=(output,),
        output_collection=output_ref,
        execution_evidence=TargetExecutionEvidence(
            target_contract_sha256=_sha("5"),
            operation_contract_sha256=_sha("4"),
            plan_sha256=evidence.execution_envelope.target_plan.plan_sha256,
            execution_sha256=_sha("a"),
        ),
        derivation=derivation.as_dict(),
    )
    return WorkRecord(
        work=work,
        phase="verifying",
        claim=ClaimBinding(claim_id="claim-1", fence=1),
        workflow_plan=workflow,
        controller_evidence=evidence,
        target_status=status,
        output=output_ref,
    )


def test_riverhog_adapter_uses_scoped_capabilities_and_verifies_settlement() -> None:
    work, workflow, target_plan, evidence = _authorities()
    api = FixtureApi()
    client = Stove0RiverhogClient(api, workspace_assurance="ephemeral")

    claim = client.acquire_claim(work)
    assert claim == ClaimBinding(claim_id="claim-1", fence=1)
    assert client.renew_claim(work, claim) == claim
    client.seal_execution(claim, evidence, workflow, target_plan)
    authority = client.target_authority(claim, evidence, target_plan)
    assert authority.workspace_assurance == "ephemeral"
    assert authority.runtime.capability_token.startswith("secret-")

    record = _verifying_record(work, workflow, evidence)
    output = client.verify_and_settle(record)
    assert output == record.output
    assert any(name == "settle" for name, _payload in api.calls)
    assert ("stream-tags", {"collection_id": output.collection_id}) in api.calls


def test_effect_target_uses_only_generic_read_custody_and_releases_without_settlement() -> None:
    work, workflow, target_plan, evidence = _effect_authorities()
    api = FixtureApi()
    client = Stove0RiverhogClient(api, workspace_assurance="ephemeral")
    claim = client.acquire_claim(work)

    client.seal_execution(claim, evidence, workflow, target_plan)
    authority = client.target_authority(claim, evidence, target_plan)
    assert authority.runtime.capability_token.startswith("secret-")
    record = WorkRecord(
        work=work,
        phase="settled",
        claim=claim,
        workflow_plan=workflow,
        target_plan=target_plan,
        controller_evidence=evidence,
    )
    client.release_claim(record)

    capability = next(payload for name, payload in api.calls if name == "capability")
    assert capability["actions"] == ("read-inputs",)
    assert capability["audience"] == "stove0.target/fixture-effect-target"
    assert not any(name in {"seal", "settle"} for name, _payload in api.calls)
    assert api.calls[-1][0] == "release"


def test_riverhog_adapter_closes_only_the_exact_generic_outcome_set() -> None:
    work, workflow, _target_plan, _evidence = _authorities()
    source_selection = ArtifactSelection.seal(
        (
            ArtifactSubject(
                id="source",
                role="fixture.source/v1",
                collection=work.inputs[0],
                path="source/input.bin",
                bytes=12,
                sha256=_sha("e"),
            ),
        )
    )
    branch = BranchPlan.build(
        parent_work=work,
        branch_id="copy",
        decision_sha256=_sha("c"),
        selection=source_selection,
        recipe=work.recipe,
        effective_intent={},
        workflow_intent=WorkflowPlanIntent.from_plan(workflow),
    )
    plan = BranchSetPlan.seal(
        parent_work=work,
        decision_sha256=_sha("c"),
        branches=(branch,),
        selections={source_selection.selection_sha256: source_selection},
    )
    output_root = CollectionRootRef(
        collection_id=7,
        archive_root_sha256=_sha("7"),
        content_identity=_sha("8"),
    )
    output_selection = ArtifactSelection.seal(
        (
            ArtifactSubject(
                id="output",
                role="fixture.output/v1",
                collection=output_root,
                path="output/result.bin",
                bytes=12,
                sha256=_sha("9"),
            ),
        )
    )
    settlement = BranchSettlement.seal(
        branch=branch,
        derivation_sha256=_sha("d"),
        output_collection=output_root,
        output_selection=output_selection,
    )
    selections = {
        source_selection.selection_sha256: source_selection,
        output_selection.selection_sha256: output_selection,
    }
    evaluation = evaluate_branch_set(
        plan,
        selections,
        branch_settlements=(settlement,),
    )
    outcome = CollectionProcessingOutcomeIdentity(
        outcome_id="branch/copy",
        source_claim_id=_sha("f"),
        output_collection=CollectionRootIdentity(
            collection_id=7,
            archive_root_sha256=_sha("7"),
            content_identity=_sha("8"),
        ),
        derivation_sha256=_sha("d"),
    )
    api = FixtureApi()
    api.processing_outcomes = [outcome.as_dict()]
    client = Stove0RiverhogClient(api)
    parent = WorkRecord(
        work=work,
        phase="coordinating",
        claim=ClaimBinding(claim_id="claim-1", fence=1),
        branch_set_plan=plan,
    )

    client.settle_outcomes(parent, evaluation)

    payload = next(payload for name, payload in api.calls if name == "settle-outcomes")
    assert payload == {
        "claim_id": "claim-1",
        "fence": 1,
        "outcomes": [outcome.as_dict()],
        "retirement_policy": "retain",
        "retirement_grace_seconds": 0,
    }


def test_riverhog_adapter_recovers_an_expired_claim_with_a_new_fence() -> None:
    work, _workflow, _target_plan, _evidence = _authorities()
    api = FixtureApi()
    client = Stove0RiverhogClient(api)
    claim = client.acquire_claim(work)
    api.expire_renewal = True

    recovered = client.renew_claim(work, claim)

    assert recovered == ClaimBinding(claim_id="claim-1", fence=2)
    assert [name for name, _payload in api.calls][-2:] == ["renew", "claim"]


def test_riverhog_adapter_refuses_to_resume_terminal_work() -> None:
    work, _workflow, _target_plan, _evidence = _authorities()
    api = FixtureApi()
    api.claim_state = "abandoned"
    client = Stove0RiverhogClient(api)

    with pytest.raises(RuntimeError, match="terminal: abandoned"):
        client.acquire_claim(work)


def test_riverhog_adapter_restarts_retryable_work_with_a_new_fence() -> None:
    work, _workflow, _target_plan, _evidence = _authorities()
    api = FixtureApi()
    client = Stove0RiverhogClient(api)
    claim = client.acquire_claim(work)

    restarted = client.restart_claim(work, claim)

    assert restarted == ClaimBinding(claim_id="claim-1", fence=2)
    assert api.calls[-1] == (
        "restart",
        {"claim_id": "claim-1", "fence": 1, "lease_seconds": 1800},
    )


def test_riverhog_adapter_retirement_is_fenced_and_challenge_bound() -> None:
    work, workflow, _target_plan, evidence = _authorities("retire-after-verified-output")
    api = FixtureApi()
    client = Stove0RiverhogClient(api)
    record = _verifying_record(work, workflow, evidence).model_copy(update={"phase": "settled"})

    assert client.begin_retirement(record) is True
    assert client.retire_input(record, 1) is True
    assert client.retire_input(record, 1) is True
    client.release_claim(record)

    delete_call = next(payload for name, payload in api.calls if name == "delete")
    assert delete_call["retirement_claim_id"] == "claim-1"
    assert delete_call["challenge"] == "delete-me"


def test_riverhog_adapter_reports_grace_and_deletion_blockers_as_waiting() -> None:
    work, workflow, _target_plan, evidence = _authorities("retire-after-verified-output")
    api = FixtureApi()
    client = Stove0RiverhogClient(api)
    record = _verifying_record(work, workflow, evidence).model_copy(update={"phase": "settled"})

    api.retirement_state = "settled"
    assert client.begin_retirement(record) is False

    api.retirement_state = "retiring"
    api.deletion_blockers = ["active retrieval"]
    assert client.begin_retirement(record) is True
    assert client.retire_input(record, 1) is False
    assert not any(name == "delete" for name, _payload in api.calls)


def test_riverhog_adapter_abandons_the_exact_claim_generation() -> None:
    work, _workflow, _target_plan, _evidence = _authorities()
    api = FixtureApi()
    client = Stove0RiverhogClient(api)
    record = WorkRecord(
        work=work,
        phase="abandon_pending",
        claim=ClaimBinding(claim_id="claim-1", fence=1),
        abandon_outcome="canceled",
    )

    client.abandon_claim(record)

    assert api.calls[-1] == (
        "abandon",
        {
            "claim_id": "claim-1",
            "fence": 1,
            "reason": "canceled: stove0 work was canceled before Riverhog settlement",
        },
    )


def test_synchronous_observation_must_fit_claim_and_capability_lifetime() -> None:
    work, _workflow, _target_plan, _evidence = _authorities()
    api = FixtureApi()
    client = Stove0RiverhogClient(
        api,
        claim_lease_seconds=30,
        capability_ttl_seconds=30,
    )
    request = ObservationRequest.seal(
        ObservationRequestPayload(
            work_id=work.work_id,
            observer_registration_id="fixture-observer",
            observer_descriptor_sha256=_sha("c"),
            observer_contract_id="fixture.observe/v1",
            observer_contract_sha256=_sha("d"),
            subjects=(
                ArtifactSubject(
                    id="source",
                    role="fixture.source/v1",
                    collection=work.inputs[0],
                    path="source/input.bin",
                    bytes=12,
                    sha256=_sha("e"),
                ),
            ),
            timeout_seconds=31,
        )
    )
    with pytest.raises(ValueError, match="timeout exceeds"):
        client.observation_authority(
            ClaimBinding(claim_id="claim-1", fence=1),
            request,
        )


def test_preview_claim_is_separate_read_only_authority_and_is_abandoned() -> None:
    work, _workflow, _target_plan, _evidence = _authorities()
    api = FixtureApi()
    client = Stove0RiverhogClient(api)
    request = WorkflowPreviewRequest.seal(WorkflowPreviewRequestPayload(work=work))

    first = client.acquire_preview_claim(request)
    client.abandon_preview_claim(request, first)
    second = client.acquire_preview_claim(request)
    client.abandon_preview_claim(request, second)

    claim_calls = [payload for name, payload in api.calls if name == "claim"]
    assert len(claim_calls) == 2
    assert claim_calls[0]["work_id"] != claim_calls[1]["work_id"]
    assert all(payload["work_id"] != request.preview_id for payload in claim_calls)
    assert {payload["purpose"] for payload in claim_calls} == {"stove0-workflow-preview/v1"}
    assert {payload["work_document"]["preview_id"] for payload in claim_calls} == {
        request.preview_id
    }
    abandon_calls = [payload for name, payload in api.calls if name == "abandon"]
    assert len(abandon_calls) == 2
    assert {payload["reason"] for payload in abandon_calls} == {
        f"preview-complete:{request.preview_id}"
    }

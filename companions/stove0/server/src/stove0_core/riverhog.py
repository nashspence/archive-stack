"""Concrete stove0 controller adapter for Riverhog's generic work API.

The adapter is intentionally narrow. It translates stove0-owned documents into
Riverhog's content-opaque claim/capability primitives and performs independent
settlement verification. It never receives or exposes archive credentials.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol

from riverhog_protocol import Conflict, NotFound
from riverhog_protocol.collection_workflows import (
    CollectionDerivation,
)
from riverhog_protocol.collection_workflows import (
    canonical_json_sha256 as riverhog_canonical_json_sha256,
)
from stove0_protocol import (
    ControllerEvidence,
    ObservationRequest,
    ObserverRuntimeAuthority,
    WorkflowPlan,
    WorkflowPreviewRequest,
    WorkIdentity,
)
from stove0_target_support import OutputCollectionRef, TargetRuntimeAuthority

from stove0_core.coordinator import TargetInvocationAuthority
from stove0_core.work_state import ClaimBinding, WorkRecord

WorkspaceAssurance = Literal["encrypted", "ephemeral"]


class RiverhogApi(Protocol):
    base_url: str
    allow_insecure_http: bool

    def create_or_resume_processing_claim(
        self,
        *,
        work_id: str,
        work_document: Mapping[str, Any],
        work_document_sha256: str,
        inputs: Sequence[Mapping[str, Any]],
        lease_seconds: int = 1800,
        purpose: str = "collection-work/v1",
    ) -> dict[str, Any]: ...

    def renew_processing_claim(
        self, claim_id: str, *, fence: int, lease_seconds: int = 1800
    ) -> dict[str, Any]: ...

    def restart_processing_claim(
        self, claim_id: str, *, fence: int, lease_seconds: int = 1800
    ) -> dict[str, Any]: ...

    def create_transform_capability(
        self,
        claim_id: str,
        *,
        fence: int,
        audience: str,
        actions: Sequence[str] = ("read-inputs",),
        ttl_seconds: int = 900,
    ) -> dict[str, Any]: ...

    def seal_processing_claim_plan(
        self,
        claim_id: str,
        *,
        fence: int,
        execution_id: str,
        controller_evidence: Mapping[str, Any],
        controller_evidence_sha256: str,
        operation_id: str,
        operation_sha256: str,
        output_tags: Sequence[str],
        retirement_policy: str = "retain",
        retirement_grace_seconds: int = 0,
    ) -> dict[str, Any]: ...

    def settle_processing_claim(
        self,
        claim_id: str,
        *,
        fence: int,
        output_collection_id: int,
        derivation: Mapping[str, Any],
    ) -> dict[str, Any]: ...

    def abandon_processing_claim(
        self, claim_id: str, *, fence: int, reason: str
    ) -> dict[str, Any]: ...

    def get_collection(self, collection_id: int) -> dict[str, Any]: ...

    def get_collection_derivation(self, collection_id: int) -> dict[str, Any]: ...

    def begin_processing_claim_retirement(self, claim_id: str, *, fence: int) -> dict[str, Any]: ...

    def plan_collection_deletion(
        self, collection_id: int, *, retirement_claim_id: str | None = None
    ) -> dict[str, Any]: ...

    def delete_collection(
        self,
        collection_id: int,
        *,
        challenge: str,
        retirement_claim_id: str | None = None,
        event_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def release_processing_claim(self, claim_id: str, *, fence: int) -> dict[str, Any]: ...


class Stove0RiverhogClient:
    """Riverhog authority used by stove0 controller and worker roles."""

    def __init__(
        self,
        api: RiverhogApi,
        *,
        claim_lease_seconds: int = 30 * 60,
        capability_ttl_seconds: int = 15 * 60,
        workspace_assurance: WorkspaceAssurance = "encrypted",
        claim_purpose: str = "stove0-collection-transform/v1",
    ) -> None:
        if claim_lease_seconds < 30 or capability_ttl_seconds < 30:
            raise ValueError("Riverhog claim and capability lifetimes must be at least 30 seconds")
        if workspace_assurance not in {"encrypted", "ephemeral"}:
            raise ValueError("stove0 workspace assurance is invalid")
        purpose = claim_purpose.strip()
        if not purpose:
            raise ValueError("Riverhog claim purpose must be visible")
        self.api = api
        self.claim_lease_seconds = claim_lease_seconds
        self.capability_ttl_seconds = capability_ttl_seconds
        self.workspace_assurance = workspace_assurance
        self.claim_purpose = purpose

    def acquire_claim(self, work: WorkIdentity) -> ClaimBinding:
        return self._acquire_document_claim(
            identity=work.work_id,
            document=work.model_dump(mode="json", by_alias=True, exclude_none=True),
            inputs=[item.model_dump(mode="json") for item in work.inputs],
            purpose=self.claim_purpose,
        )

    def acquire_preview_claim(self, request: WorkflowPreviewRequest) -> ClaimBinding:
        # The preview result identity is semantic and repeatable, but each read-only
        # execution receives a distinct Riverhog claim. A completed preview abandons
        # its claim, so reusing the semantic preview ID as claim work identity would
        # collide with that terminal claim on a later preview invocation.
        attempt_id = secrets.token_hex(16)
        claim_work_id = riverhog_canonical_json_sha256(
            {
                "format": "stove0-workflow-preview-claim/v1",
                "preview_id": request.preview_id,
                "attempt_id": attempt_id,
            }
        )
        return self._acquire_document_claim(
            identity=claim_work_id,
            document=request.model_dump(mode="json", by_alias=True, exclude_none=True),
            inputs=[item.model_dump(mode="json") for item in request.work.inputs],
            purpose="stove0-workflow-preview/v1",
        )

    def renew_claim(
        self,
        work: WorkIdentity,
        claim: ClaimBinding,
    ) -> ClaimBinding:
        try:
            payload = self.api.renew_processing_claim(
                claim.claim_id,
                fence=claim.fence,
                lease_seconds=self.claim_lease_seconds,
            )
        except Conflict as exc:
            recovered = self.acquire_claim(work)
            if recovered.claim_id != claim.claim_id or recovered.fence <= claim.fence:
                raise RuntimeError(
                    "Riverhog did not recover the expired claim with a newer fence"
                ) from exc
            return recovered
        renewed = _claim_binding(payload)
        if renewed != claim:
            raise RuntimeError("Riverhog renewed a different claim generation")
        return renewed

    def restart_claim(
        self,
        work: WorkIdentity,
        claim: ClaimBinding,
    ) -> ClaimBinding:
        try:
            payload = self.api.restart_processing_claim(
                claim.claim_id,
                fence=claim.fence,
                lease_seconds=self.claim_lease_seconds,
            )
        except Conflict as exc:
            recovered = self.acquire_claim(work)
            if recovered.claim_id != claim.claim_id or recovered.fence <= claim.fence:
                raise RuntimeError(
                    "Riverhog did not reconcile the restarted claim with a newer fence"
                ) from exc
            return recovered
        restarted = _claim_binding(payload)
        if restarted.claim_id != claim.claim_id or restarted.fence != claim.fence + 1:
            raise RuntimeError("Riverhog restarted an unexpected claim generation")
        return restarted

    def observation_authority(
        self,
        claim: ClaimBinding,
        request: ObservationRequest,
    ) -> ObserverRuntimeAuthority:
        if request.timeout_seconds > min(
            self.claim_lease_seconds,
            self.capability_ttl_seconds,
        ):
            raise ValueError(
                "synchronous observation timeout exceeds its claim/capability lifetime"
            )
        audience = f"stove0.observer/{request.observer_registration_id}"
        capability = self._capability(
            claim,
            audience=audience,
            actions=("read-inputs",),
        )
        return ObserverRuntimeAuthority(
            riverhog_base_url=self.api.base_url,
            capability_token=_token(capability),
            allow_insecure_http=bool(self.api.allow_insecure_http),
            workspace_assurance=self.workspace_assurance,
        )

    def seal_execution(
        self,
        claim: ClaimBinding,
        evidence: ControllerEvidence,
        plan: WorkflowPlan,
    ) -> None:
        envelope = evidence.execution_envelope
        if envelope.workflow_plan != plan:
            raise ValueError("controller evidence does not contain the selected workflow plan")
        if envelope.claim_id != claim.claim_id or envelope.fence != claim.fence:
            raise ValueError("controller evidence differs from the current Riverhog claim")
        document = evidence.model_dump(mode="json", by_alias=True, exclude_none=True)
        payload = self.api.seal_processing_claim_plan(
            claim.claim_id,
            fence=claim.fence,
            execution_id=envelope.execution_envelope_sha256,
            controller_evidence=document,
            controller_evidence_sha256=riverhog_canonical_json_sha256(document),
            operation_id=plan.operation.id,
            operation_sha256=plan.operation.sha256,
            output_tags=plan.output_tags,
            retirement_policy=plan.retirement_policy,
            retirement_grace_seconds=plan.retirement_grace_seconds,
        )
        binding = _claim_binding(payload)
        if binding != claim:
            raise RuntimeError("Riverhog sealed a different claim generation")
        sealed = payload.get("plan")
        if not isinstance(sealed, Mapping) or sealed.get("execution_id") != (
            envelope.execution_envelope_sha256
        ):
            raise RuntimeError("Riverhog did not retain the sealed execution identity")

    def target_authority(
        self,
        claim: ClaimBinding,
        evidence: ControllerEvidence,
    ) -> TargetInvocationAuthority:
        envelope = evidence.execution_envelope
        if envelope.claim_id != claim.claim_id or envelope.fence != claim.fence:
            raise ValueError("target evidence differs from the current Riverhog claim")
        execution_id = envelope.execution_envelope_sha256
        capability = self._capability(
            claim,
            audience=("stove0.target/" + envelope.workflow_plan.target_registration_id),
            actions=("read-inputs", "write-output"),
        )
        principal = capability.get("principal_app")
        if principal != f"transform:{execution_id}":
            raise RuntimeError("Riverhog output capability has an unexpected principal")
        return TargetInvocationAuthority(
            runtime=TargetRuntimeAuthority(
                riverhog_base_url=self.api.base_url,
                capability_token=_token(capability),
                allow_insecure_http=bool(self.api.allow_insecure_http),
            ),
            workspace_assurance=self.workspace_assurance,
        )

    def verify_and_settle(self, record: WorkRecord) -> OutputCollectionRef:
        if (
            record.phase != "verifying"
            or record.claim is None
            or record.workflow_plan is None
            or record.controller_evidence is None
            or record.target_status is None
            or record.target_status.state != "succeeded"
            or record.target_status.output_collection is None
            or record.target_status.derivation is None
        ):
            raise ValueError("stove0 work is not ready for Riverhog settlement")
        target_output = record.target_status.output_collection
        derivation = CollectionDerivation.from_mapping(record.target_status.derivation)
        self.api.settle_processing_claim(
            record.claim.claim_id,
            fence=record.claim.fence,
            output_collection_id=target_output.collection_id,
            derivation=derivation.as_dict(),
        )
        collection = self.api.get_collection(target_output.collection_id)
        stored = self.api.get_collection_derivation(target_output.collection_id)
        stored_document = stored.get("derivation")
        if not isinstance(stored_document, Mapping):
            raise RuntimeError("Riverhog returned no immutable derivation document")
        verified = CollectionDerivation.from_mapping(stored_document)
        if verified != derivation or stored.get("document_sha256") != derivation.sha256:
            raise RuntimeError("Riverhog derivation differs from the target publication evidence")
        tags = tuple(sorted(str(item) for item in collection.get("tags", ())))
        if tags != record.workflow_plan.output_tags:
            raise RuntimeError("Riverhog output tags differ from the sealed stove0 plan")
        output = OutputCollectionRef(
            collection_id=_positive_int(collection.get("id"), "collection id"),
            manifest_sha256=_text(collection.get("manifest_sha256"), "manifest identity"),
            content_etag=_text(collection.get("content_etag"), "content identity"),
            derivation_sha256=derivation.sha256,
        )
        if output != target_output:
            raise RuntimeError("Riverhog output root differs from the target publication receipt")
        return output

    def abandon_preview_claim(
        self,
        request: WorkflowPreviewRequest,
        claim: ClaimBinding,
    ) -> None:
        payload = self.api.abandon_processing_claim(
            claim.claim_id,
            fence=claim.fence,
            reason=f"preview-complete:{request.preview_id}",
        )
        if payload.get("state") != "abandoned" or _claim_binding(payload) != claim:
            raise RuntimeError("Riverhog did not abandon the expected preview claim")

    def begin_retirement(self, record: WorkRecord) -> None:
        claim = _record_claim(record)
        payload = self.api.begin_processing_claim_retirement(
            claim.claim_id,
            fence=claim.fence,
        )
        if payload.get("state") != "retiring" or _claim_binding(payload) != claim:
            raise RuntimeError("Riverhog did not enter the expected retirement claim")

    def abandon_claim(self, record: WorkRecord) -> None:
        claim = _record_claim(record)
        payload = self.api.abandon_processing_claim(
            claim.claim_id,
            fence=claim.fence,
            reason=_abandonment_reason(record),
        )
        if payload.get("state") != "abandoned" or _claim_binding(payload) != claim:
            raise RuntimeError("Riverhog did not abandon the expected processing claim")

    def retire_input(self, record: WorkRecord, collection_id: int) -> None:
        claim = _record_claim(record)
        if int(collection_id) not in {item.collection_id for item in record.work.inputs}:
            raise ValueError("retirement collection is outside the stove0 work")
        try:
            plan = self.api.plan_collection_deletion(
                int(collection_id),
                retirement_claim_id=claim.claim_id,
            )
        except NotFound:
            # A prior attempt may have deleted the exact immutable input before
            # stove0 durably recorded the phase transition. Absence is the
            # idempotent success condition; Riverhog still gates final release.
            return
        blockers = plan.get("blockers")
        challenge = plan.get("challenge")
        if blockers:
            raise RuntimeError(f"Riverhog retirement is blocked: {blockers}")
        if plan.get("status") != "ready" or not isinstance(challenge, str) or not challenge:
            raise RuntimeError("Riverhog did not return a ready retirement deletion plan")
        result = self.api.delete_collection(
            int(collection_id),
            challenge=challenge,
            retirement_claim_id=claim.claim_id,
            event_context={
                "initiator": {
                    "app": "stove0",
                    "claim_id": claim.claim_id,
                    "fence": claim.fence,
                    "work_id": record.work_id,
                }
            },
        )
        if result.get("status") not in {"deleted", "already_absent"}:
            raise RuntimeError("Riverhog did not confirm input retirement")

    def release_claim(self, record: WorkRecord) -> None:
        claim = _record_claim(record)
        payload = self.api.release_processing_claim(
            claim.claim_id,
            fence=claim.fence,
        )
        if payload.get("state") != "released" or _claim_binding(payload) != claim:
            raise RuntimeError("Riverhog did not release the expected claim")

    def _acquire_document_claim(
        self,
        *,
        identity: str,
        document: Mapping[str, object],
        inputs: Sequence[Mapping[str, object]],
        purpose: str,
    ) -> ClaimBinding:
        payload = self.api.create_or_resume_processing_claim(
            work_id=identity,
            work_document=dict(document),
            work_document_sha256=riverhog_canonical_json_sha256(document),
            inputs=[dict(item) for item in inputs],
            lease_seconds=self.claim_lease_seconds,
            purpose=purpose,
        )
        if payload.get("work_id") != identity:
            raise RuntimeError("Riverhog claim differs from the requested identity")
        state = str(payload.get("state") or "")
        if state != "active":
            raise RuntimeError(f"Riverhog processing claim is terminal: {state or 'unknown'}")
        return _claim_binding(payload)

    def _capability(
        self,
        claim: ClaimBinding,
        *,
        audience: str,
        actions: Sequence[str],
    ) -> dict[str, Any]:
        payload = self.api.create_transform_capability(
            claim.claim_id,
            fence=claim.fence,
            audience=audience,
            actions=tuple(actions),
            ttl_seconds=self.capability_ttl_seconds,
        )
        if (
            payload.get("claim_id") != claim.claim_id
            or payload.get("fence") != claim.fence
            or payload.get("audience") != audience
            or tuple(payload.get("actions", ())) != tuple(sorted(set(actions)))
        ):
            raise RuntimeError("Riverhog returned an inconsistent scoped capability")
        return payload


def _record_claim(record: WorkRecord) -> ClaimBinding:
    if record.claim is None:
        raise ValueError("stove0 work has no Riverhog claim")
    return record.claim


def _abandonment_reason(record: WorkRecord) -> str:
    outcome = record.abandon_outcome
    if outcome == "inapplicable" and record.inapplicable is not None:
        return f"inapplicable:{record.inapplicable.code}: {record.inapplicable.message}"
    if outcome == "failed" and record.failure is not None:
        return f"failed:{record.failure.code}: {record.failure.message}"
    if outcome == "canceled":
        return "canceled: stove0 work was canceled before Riverhog settlement"
    raise ValueError("stove0 work has no terminal claim-abandonment outcome")


def _claim_binding(value: Mapping[str, Any]) -> ClaimBinding:
    return ClaimBinding(
        claim_id=_text(value.get("id"), "claim id"),
        fence=_positive_int(value.get("fence"), "claim fence"),
    )


def _token(value: Mapping[str, Any]) -> str:
    return _text(value.get("token"), "capability token")


def _text(value: object, label: str) -> str:
    text = str(value or "")
    if not text or text != text.strip():
        raise RuntimeError(f"Riverhog returned an invalid {label}")
    return text


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise RuntimeError(f"Riverhog returned an invalid {label}")
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Riverhog returned an invalid {label}") from exc
    if parsed < 1:
        raise RuntimeError(f"Riverhog returned an invalid {label}")
    return parsed


__all__ = ["RiverhogApi", "Stove0RiverhogClient", "WorkspaceAssurance"]

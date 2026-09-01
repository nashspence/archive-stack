"""Concrete stove0 controller adapter for Riverhog's generic work API.

The adapter is intentionally narrow. It translates stove0-owned documents into
Riverhog's content-opaque claim/capability primitives and performs independent
settlement verification. It never receives or exposes archive credentials.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal, Protocol

from riverhog_protocol import Conflict, NotFound
from riverhog_protocol.collection_workflow_transport import (
    DISPOSITION_BATCH_MAX,
    ArtifactDispositionOutputPageDocument,
    ArtifactDispositionPageDocument,
    ArtifactDispositionSetDocument,
    CapabilityAction,
    CollectionDerivationResponseDocument,
    ProcessingClaimDocument,
    ProcessingOutcomePageDocument,
    TransformCapabilityDocument,
)
from riverhog_protocol.collection_workflows import (
    ArtifactDisposition,
    ArtifactDispositionOutput,
    ArtifactDispositionSetIdentity,
    CollectionArtifactIdentity,
    CollectionDerivation,
    CollectionProcessingOutcomeIdentity,
    CollectionRootIdentity,
    RetirementPolicy,
)
from riverhog_protocol.collection_workflows import (
    canonical_json_sha256 as riverhog_canonical_json_sha256,
)
from riverhog_protocol.paths import tag_set_identity
from riverhog_protocol.portable_collection import PortableCollectionInventoryPage
from stove0_observer_protocol import ObservationRequest, ObserverRuntimeAuthority
from stove0_protocol import (
    ArtifactSubject,
    BranchSetEvaluation,
    ControllerEvidence,
    TargetPlanBinding,
    WorkflowPlan,
    WorkflowPreviewRequest,
    WorkIdentity,
)
from stove0_target_protocol import (
    InputArtifact,
    OutputCollectionRef,
    TargetOutputBinding,
    TargetOutputBindingSetIdentity,
    TargetPlan,
    TargetRuntimeAuthority,
    TargetSettlementAuthority,
    TargetSettlementAuthorityPayload,
    update_target_output_binding_commitment,
)

from stove0_core._checkpoint_sha256 import CheckpointSHA256
from stove0_core.coordinator import (
    ParentOutcomeBinding,
    TargetInvocationAuthority,
)
from stove0_core.work_state import (
    ClaimBinding,
    ConcurrentWorkUpdate,
    TargetSettlementSealCheckpoint,
    TargetSettlementSealRecord,
    WorkRecord,
    WorkStore,
)

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
        inputs: Iterable[Mapping[str, Any]],
        lease_seconds: int = 1800,
        purpose: str = "collection-work/v1",
    ) -> ProcessingClaimDocument: ...

    def renew_processing_claim(
        self, claim_id: str, *, fence: int, lease_seconds: int = 1800
    ) -> ProcessingClaimDocument: ...

    def restart_processing_claim(
        self, claim_id: str, *, fence: int, lease_seconds: int = 1800
    ) -> ProcessingClaimDocument: ...

    def get_processing_claim(self, claim_id: str) -> ProcessingClaimDocument: ...

    def create_transform_capability(
        self,
        claim_id: str,
        *,
        fence: int,
        audience: str,
        actions: Sequence[CapabilityAction] = ("read-inputs",),
        artifacts: Iterable[Mapping[str, Any]],
        ttl_seconds: int = 900,
    ) -> TransformCapabilityDocument: ...

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
        input_artifacts: Iterable[Mapping[str, Any]],
        output_tags: Sequence[str],
        retirement_policy: RetirementPolicy = "retain",
        retirement_grace_seconds: int = 0,
    ) -> ProcessingClaimDocument: ...

    def settle_processing_claim(
        self,
        claim_id: str,
        *,
        fence: int,
        output_collection_id: int,
        derivation: Mapping[str, Any],
        outcome_claim_id: str | None = None,
        outcome_fence: int | None = None,
        outcome_id: str | None = None,
    ) -> ProcessingClaimDocument: ...

    def settle_processing_claim_outcomes(
        self,
        claim_id: str,
        *,
        fence: int,
        retirement_policy: RetirementPolicy = "retain",
        retirement_grace_seconds: int = 0,
    ) -> ProcessingClaimDocument: ...

    def list_processing_claim_outcomes(
        self,
        claim_id: str,
        *,
        authority_sha256: str,
        start_ordinal: int = 0,
    ) -> ProcessingOutcomePageDocument: ...

    def abandon_processing_claim(
        self, claim_id: str, *, fence: int, reason: str
    ) -> ProcessingClaimDocument: ...

    def get_collection(self, collection_id: int) -> dict[str, Any]: ...

    def get_collection_tags(
        self,
        collection_id: int,
        *,
        page_size: int = 25,
        page_token: str | None = None,
    ) -> dict[str, Any]: ...

    def get_collection_derivation(
        self, collection_id: int
    ) -> CollectionDerivationResponseDocument: ...

    def get_portable_collection_inventory(
        self,
        collection_id: int,
        *,
        cursor: str | None = None,
        limit: int = 100,
        inventory_identity: str | None = None,
    ) -> PortableCollectionInventoryPage: ...

    def list_processing_claim_dispositions(
        self,
        claim_id: str,
        *,
        authority_sha256: str,
        start_ordinal: int = 0,
    ) -> ArtifactDispositionPageDocument: ...

    def list_processing_claim_disposition_outputs(
        self,
        claim_id: str,
        *,
        authority_sha256: str,
        start_ordinal: int = 0,
    ) -> ArtifactDispositionOutputPageDocument: ...

    def record_processing_claim_dispositions(
        self,
        claim_id: str,
        *,
        fence: int,
        dispositions: Sequence[Mapping[str, Any]],
    ) -> ArtifactDispositionSetDocument: ...

    def record_processing_claim_disposition_outputs(
        self,
        claim_id: str,
        *,
        fence: int,
        outputs: Sequence[Mapping[str, Any]],
    ) -> ArtifactDispositionSetDocument: ...

    def seal_processing_claim_dispositions(
        self, claim_id: str, *, fence: int
    ) -> ArtifactDispositionSetDocument: ...

    def get_processing_claim_dispositions(
        self, claim_id: str
    ) -> ArtifactDispositionSetDocument: ...

    def begin_processing_claim_retirement(
        self, claim_id: str, *, fence: int
    ) -> ProcessingClaimDocument: ...

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

    def release_processing_claim(self, claim_id: str, *, fence: int) -> ProcessingClaimDocument: ...


def _verify_disposition_authority(
    api: RiverhogApi,
    record: WorkRecord,
    derivation: CollectionDerivation,
) -> None:
    assert record.claim is not None
    assert record.target_status is not None
    assert record.target_status.production is not None
    production = record.target_status.production
    if derivation.disposition_set != production.riverhog_disposition_set:
        raise RuntimeError("target derivation differs from its production authority")
    sealed = api.get_processing_claim_dispositions(record.claim.claim_id)
    if (
        sealed.state != "sealed"
        or sealed.identity is None
        or ArtifactDispositionSetIdentity.from_mapping(sealed.identity.model_dump(mode="json"))
        != production.riverhog_disposition_set
    ):
        raise RuntimeError("Riverhog generic derivation authority changed")


def _collection_tags(api: RiverhogApi, collection_id: int) -> tuple[str, ...]:
    page_token: str | None = None
    authority: tuple[int, str] | None = None
    tags: list[str] = []
    while True:
        payload = api.get_collection_tags(
            collection_id,
            page_size=100,
            page_token=page_token,
        )
        current = (
            int(payload.get("metadata_revision") or 0),
            str(payload.get("inventory_identity") or ""),
        )
        if authority is None:
            authority = current
        elif authority != current:
            raise RuntimeError("Riverhog collection tags changed during bounded traversal")
        raw_tags = payload.get("tags")
        if not isinstance(raw_tags, list):
            raise RuntimeError("Riverhog returned invalid collection tags")
        tags.extend(str(tag) for tag in raw_tags)
        next_page_token = payload.get("next_page_token")
        if next_page_token is None:
            break
        if not isinstance(next_page_token, str) or not next_page_token:
            raise RuntimeError("Riverhog returned an invalid collection-tag page token")
        page_token = next_page_token
    if authority is None or tags != sorted(set(tags)):
        raise RuntimeError("Riverhog collection tag traversal is invalid")
    return tuple(tags)


class Stove0RiverhogClient:
    """Riverhog authority used by stove0 controller and worker roles."""

    def __init__(
        self,
        api: RiverhogApi,
        *,
        claim_lease_seconds: int = 30 * 60,
        capability_ttl_seconds: int = 15 * 60,
        workspace_assurance: WorkspaceAssurance = "encrypted",
        claim_purpose: str = "stove0-collection-work/v1",
        state: WorkStore | None = None,
        authority_batch_size: int = 100,
    ) -> None:
        if claim_lease_seconds < 30 or capability_ttl_seconds < 30:
            raise ValueError("Riverhog claim and capability lifetimes must be at least 30 seconds")
        if workspace_assurance not in {"encrypted", "ephemeral"}:
            raise ValueError("stove0 workspace assurance is invalid")
        purpose = claim_purpose.strip()
        if not purpose:
            raise ValueError("Riverhog claim purpose must be visible")
        if authority_batch_size < 1 or authority_batch_size > DISPOSITION_BATCH_MAX:
            raise ValueError(
                f"Stove0 authority batch size must be between 1 and {DISPOSITION_BATCH_MAX}"
            )
        self.api = api
        self.claim_lease_seconds = claim_lease_seconds
        self.capability_ttl_seconds = capability_ttl_seconds
        self.workspace_assurance = workspace_assurance
        self.claim_purpose = purpose
        self.state = state
        self.authority_batch_size = authority_batch_size

    def project_target_dispositions(
        self,
        record: WorkRecord,
        dispositions: Sequence[ArtifactDisposition],
    ) -> None:
        if record.claim is None:
            raise ValueError("target production requires an active Riverhog claim")
        if not dispositions or len(dispositions) > DISPOSITION_BATCH_MAX:
            raise ValueError("target disposition projection batch is invalid")
        self.api.record_processing_claim_dispositions(
            record.claim.claim_id,
            fence=record.claim.fence,
            dispositions=[item.as_dict() for item in dispositions],
        )

    def project_target_source_edges(
        self,
        record: WorkRecord,
        edges: Sequence[ArtifactDispositionOutput],
    ) -> None:
        if record.claim is None:
            raise ValueError("target production requires an active Riverhog claim")
        if not edges or len(edges) > DISPOSITION_BATCH_MAX:
            raise ValueError("target source-edge projection batch is invalid")
        self.api.record_processing_claim_disposition_outputs(
            record.claim.claim_id,
            fence=record.claim.fence,
            outputs=[item.as_dict() for item in edges],
        )

    def seal_target_projection(
        self,
        record: WorkRecord,
    ) -> ArtifactDispositionSetIdentity | None:
        if record.claim is None:
            raise ValueError("target production requires an active Riverhog claim")
        status = self.api.seal_processing_claim_dispositions(
            record.claim.claim_id,
            fence=record.claim.fence,
        )
        if status.state == "sealing":
            return None
        if status.state != "sealed" or status.identity is None:
            raise RuntimeError(status.failure or "Riverhog did not seal derivation evidence")
        return ArtifactDispositionSetIdentity.from_mapping(status.identity.model_dump(mode="json"))

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
            # Observation subjects are semantically ordered by their request-scoped
            # IDs.  Capability scope is a different, generic Riverhog authority and
            # must be projected into immutable collection-artifact order.
            artifacts=tuple(sorted(_artifact_identity(item) for item in request.subjects)),
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
        target_plan: TargetPlan,
        inputs: Iterable[ArtifactSubject],
    ) -> None:
        envelope = evidence.execution_envelope
        if envelope.workflow_plan != plan:
            raise ValueError("controller evidence does not contain the selected workflow plan")
        if envelope.claim_id != claim.claim_id or envelope.fence != claim.fence:
            raise ValueError("controller evidence differs from the current Riverhog claim")
        if _target_binding(target_plan) != envelope.target_plan:
            raise ValueError("controller evidence differs from the exact target plan")
        if plan.result_kind == "external-effect":
            return
        document = evidence.model_dump(mode="json", by_alias=True, exclude_none=True)
        payload = self.api.seal_processing_claim_plan(
            claim.claim_id,
            fence=claim.fence,
            execution_id=envelope.execution_envelope_sha256,
            controller_evidence=document,
            controller_evidence_sha256=riverhog_canonical_json_sha256(document),
            operation_id=plan.operation.id,
            operation_sha256=plan.operation.sha256,
            input_artifacts=(_artifact_identity(item).as_dict() for item in inputs),
            output_tags=plan.output_tags,
            retirement_policy=plan.retirement_policy,
            retirement_grace_seconds=plan.retirement_grace_seconds,
        )
        binding = _claim_binding(payload)
        if binding != claim:
            raise RuntimeError("Riverhog sealed a different claim generation")
        sealed = payload.get("plan")
        if sealed is None or sealed.get("execution_id") != envelope.execution_envelope_sha256:
            raise RuntimeError("Riverhog did not retain the sealed execution identity")

    def target_authority(
        self,
        claim: ClaimBinding,
        evidence: ControllerEvidence,
        target_plan: TargetPlan,
        inputs: Iterable[ArtifactSubject],
    ) -> TargetInvocationAuthority:
        envelope = evidence.execution_envelope
        if envelope.claim_id != claim.claim_id or envelope.fence != claim.fence:
            raise ValueError("target evidence differs from the current Riverhog claim")
        if _target_binding(target_plan) != envelope.target_plan:
            raise ValueError("target evidence differs from the exact target plan")
        execution_id = envelope.execution_envelope_sha256
        actions: tuple[CapabilityAction, ...] = (
            ("read-inputs",)
            if envelope.workflow_plan.result_kind == "external-effect"
            else ("read-inputs", "write-output")
        )
        capability = self._capability(
            claim,
            audience=("stove0.target/" + envelope.workflow_plan.target_registration_id),
            actions=actions,
            artifacts=(_artifact_identity(item) for item in inputs),
        )
        principal = capability.get("principal_app")
        expected_principal = (
            f"claim:{claim.claim_id}"
            if envelope.workflow_plan.result_kind == "external-effect"
            else f"transform:{execution_id}"
        )
        if principal != expected_principal:
            raise RuntimeError("Riverhog target capability has an unexpected principal")
        return TargetInvocationAuthority(
            runtime=TargetRuntimeAuthority(
                riverhog_base_url=self.api.base_url,
                capability_token=_token(capability),
                allow_insecure_http=bool(self.api.allow_insecure_http),
            ),
            workspace_assurance=self.workspace_assurance,
        )

    def verify_and_settle(
        self,
        record: WorkRecord,
        parent_outcome: ParentOutcomeBinding | None = None,
    ) -> tuple[OutputCollectionRef, TargetSettlementAuthority | None]:
        if (
            record.phase != "verifying"
            or record.claim is None
            or record.workflow_plan is None
            or record.controller_evidence is None
            or record.target_status is None
            or record.target_status.state != "succeeded"
            or record.target_status.output_collection is None
            or record.target_status.derivation is None
            or record.target_status.production is None
        ):
            raise ValueError("stove0 work is not ready for Riverhog settlement")
        target_output = record.target_status.output_collection
        derivation = CollectionDerivation.from_mapping(record.target_status.derivation)
        _verify_disposition_authority(
            self.api,
            record,
            derivation,
        )
        self.api.settle_processing_claim(
            record.claim.claim_id,
            fence=record.claim.fence,
            output_collection_id=target_output.collection_id,
            derivation=derivation.as_dict(),
            outcome_claim_id=(parent_outcome.claim.claim_id if parent_outcome else None),
            outcome_fence=(parent_outcome.claim.fence if parent_outcome else None),
            outcome_id=(parent_outcome.outcome_id if parent_outcome else None),
        )
        collection = self.api.get_collection(target_output.collection_id)
        stored = self.api.get_collection_derivation(target_output.collection_id)
        stored_document = stored.get("derivation")
        if not isinstance(stored_document, Mapping):
            raise RuntimeError("Riverhog returned no immutable derivation document")
        verified = CollectionDerivation.from_mapping(stored_document)
        if verified != derivation or stored.get("document_sha256") != derivation.sha256:
            raise RuntimeError("Riverhog derivation differs from the target publication evidence")
        if collection.get("tag_set_identity") != tag_set_identity(record.workflow_plan.output_tags):
            raise RuntimeError("Riverhog output tags differ from the sealed stove0 plan")
        output = OutputCollectionRef(
            collection_id=_positive_int(collection.get("id"), "collection id"),
            archive_root_sha256=_text(
                collection.get("archive_root_sha256"),
                "archive-root identity",
            ),
            content_identity=_text(collection.get("content_identity"), "content identity"),
            derivation_sha256=derivation.sha256,
        )
        if output != target_output:
            raise RuntimeError("Riverhog output root differs from the target publication receipt")
        settlement = self._advance_settlement(record, output)
        return output, settlement

    def _advance_settlement(
        self,
        record: WorkRecord,
        output: OutputCollectionRef,
    ) -> TargetSettlementAuthority | None:
        assert record.target_status is not None
        assert record.target_status.production is not None
        production = record.target_status.production
        if self.state is None:
            raise RuntimeError("Stove0 settlement requires its durable state authority")
        seal = self.state.ensure_target_settlement_binding(
            TargetSettlementSealRecord(
                work_id=record.work_id,
                job_id=production.job_id,
                output_collection=output,
                production_sha256=production.production_sha256,
                checkpoint=TargetSettlementSealCheckpoint(
                    binding_hash_state=CheckpointSHA256().export_state()
                ),
            )
        )
        if seal.state == "failed":
            raise RuntimeError(seal.failure or "target settlement binding failed")
        if seal.state == "sealed":
            return seal.settlement
        checkpoint = seal.checkpoint
        assert checkpoint is not None
        page = self.api.get_portable_collection_inventory(
            output.collection_id,
            cursor=checkpoint.inventory_cursor,
            limit=self.authority_batch_size,
            inventory_identity=checkpoint.inventory_identity,
        )
        authority = page.authority
        inventory_identity = checkpoint.inventory_identity or authority.inventory_identity
        if (
            authority.inventory_identity != inventory_identity
            or authority.header.collection != output.collection_id
            or authority.header.content_identity != output.content_identity
        ):
            raise RuntimeError("Riverhog output inventory changed during settlement")
        files = tuple(file for file in page.files if not file.path.startswith("riverhog/"))
        declarations = (
            self.state.target_output_path_page(
                record.work_id,
                production.job_id,
                after_path=checkpoint.output_path_cursor,
                limit=len(files),
            )
            if files
            else ()
        )
        if len(declarations) != len(files):
            raise RuntimeError("Riverhog output collection differs from target production")
        digest = CheckpointSHA256.from_state(checkpoint.binding_hash_state)
        artifact_count = checkpoint.artifact_count
        total_bytes = checkpoint.total_bytes
        output_path_cursor = checkpoint.output_path_cursor
        for declared, file in zip(declarations, files, strict=True):
            if (
                declared.path != file.path
                or declared.bytes != file.bytes
                or declared.sha256 != file.sha256
            ):
                raise RuntimeError("Riverhog artifact differs from its target declaration")
            binding = TargetOutputBinding(
                output_id=declared.id,
                role=declared.role,
                collection=output,
                path=declared.path,
                bytes=declared.bytes,
                sha256=declared.sha256,
                media_type=declared.media_type,
            )
            update_target_output_binding_commitment(
                digest,
                ordinal=artifact_count,
                binding=binding,
            )
            artifact_count += 1
            total_bytes += declared.bytes
            output_path_cursor = declared.path
        if not page.complete and page.next_cursor is None:
            raise RuntimeError("Riverhog output inventory ended without completion")
        next_checkpoint = TargetSettlementSealCheckpoint(
            inventory_identity=inventory_identity,
            inventory_cursor=page.next_cursor,
            output_path_cursor=output_path_cursor,
            binding_hash_state=digest.export_state(),
            artifact_count=artifact_count,
            total_bytes=total_bytes,
        )
        settlement: TargetSettlementAuthority | None = None
        if page.complete:
            if self.state.target_output_path_page(
                record.work_id,
                production.job_id,
                after_path=output_path_cursor,
                limit=1,
            ) or (
                artifact_count != production.outputs.artifact_count
                or total_bytes != production.outputs.total_bytes
            ):
                raise RuntimeError("post-root output bindings differ from target production")
            settlement = TargetSettlementAuthority.seal(
                TargetSettlementAuthorityPayload(
                    job_id=production.job_id,
                    production_sha256=production.production_sha256,
                    output_collection=output,
                    output_bindings=TargetOutputBindingSetIdentity(
                        artifact_count=artifact_count,
                        total_bytes=total_bytes,
                        sha256=digest.hexdigest(),
                    ),
                )
            )
        replacement = TargetSettlementSealRecord.model_validate(
            seal.model_copy(
                update={
                    "revision": seal.revision + 1,
                    "state": "sealed" if settlement is not None else "binding",
                    "checkpoint": None if settlement is not None else next_checkpoint,
                    "settlement": settlement,
                }
            ).model_dump(mode="python")
        )
        try:
            sealed = self.state.compare_and_swap_target_settlement_seal(
                record.work_id,
                production.job_id,
                expected_revision=seal.revision,
                replacement=replacement,
            )
        except ConcurrentWorkUpdate:
            concurrent = self.state.load_target_settlement_seal(record.work_id, production.job_id)
            if concurrent is None:
                raise RuntimeError("target settlement binding disappeared") from None
            if (
                concurrent.output_collection != output
                or concurrent.production_sha256 != production.production_sha256
            ):
                raise RuntimeError("target settlement binding changed") from None
            sealed = concurrent
        return sealed.settlement

    def settle_outcomes(
        self,
        record: WorkRecord,
        evaluation: BranchSetEvaluation,
    ) -> bool:
        if (
            record.claim is None
            or record.branch_set_plan is None
            or not evaluation.branch_set_succeeded
            or evaluation.branch_set_sha256 != record.branch_set_plan.branch_set_sha256
        ):
            raise ValueError("stove0 coordination is not ready for Riverhog settlement")
        payload = self.api.settle_processing_claim_outcomes(
            record.claim.claim_id,
            fence=record.claim.fence,
            retirement_policy=record.branch_set_plan.retirement_policy,
            retirement_grace_seconds=record.branch_set_plan.retirement_grace_seconds,
        )
        if payload.state == "active":
            return False
        if payload.state != "settled" or payload.outcomes.authority is None:
            raise RuntimeError("Riverhog did not seal the processing outcome authority")
        outcomes: list[CollectionProcessingOutcomeIdentity] = []
        ordinal = 0
        while True:
            page = self.api.list_processing_claim_outcomes(
                record.claim.claim_id,
                authority_sha256=payload.outcomes.authority.sha256,
                start_ordinal=ordinal,
            )
            outcomes.extend(
                CollectionProcessingOutcomeIdentity.from_mapping(item.model_dump(mode="json"))
                for item in page.outcomes
            )
            if page.next_ordinal is None:
                break
            ordinal = page.next_ordinal
        expected: dict[str, tuple[CollectionRootIdentity, str]] = {
            f"branch/{item.branch_id}": (
                CollectionRootIdentity(
                    collection_id=item.output_collection.collection_id,
                    archive_root_sha256=item.output_collection.archive_root_sha256,
                    content_identity=item.output_collection.content_identity,
                ),
                item.derivation_sha256,
            )
            for item in evaluation.succeeded_branches
        }
        if evaluation.join_settlement is not None:
            join = evaluation.join_settlement
            expected["join"] = (
                CollectionRootIdentity(
                    collection_id=join.output_collection.collection_id,
                    archive_root_sha256=join.output_collection.archive_root_sha256,
                    content_identity=join.output_collection.content_identity,
                ),
                join.derivation_sha256,
            )
        actual = {
            item.outcome_id: (item.output_collection, item.derivation_sha256) for item in outcomes
        }
        if actual != expected:
            raise RuntimeError("Riverhog processing outcomes differ from Stove0 truth")
        if _claim_binding(payload) != record.claim:
            raise RuntimeError("Riverhog did not settle the expected processing outcomes")
        return True

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

    def begin_retirement(self, record: WorkRecord) -> bool:
        claim = _record_claim(record)
        payload = self.api.begin_processing_claim_retirement(
            claim.claim_id,
            fence=claim.fence,
        )
        if _claim_binding(payload) != claim:
            raise RuntimeError("Riverhog returned another retirement claim")
        state = payload.get("state")
        if state == "settled":
            return False
        if state != "retiring":
            raise RuntimeError("Riverhog did not enter the expected retirement claim")
        return True

    def abandon_claim(self, record: WorkRecord) -> None:
        claim = _record_claim(record)
        payload = self.api.abandon_processing_claim(
            claim.claim_id,
            fence=claim.fence,
            reason=_abandonment_reason(record),
        )
        if payload.get("state") != "abandoned" or _claim_binding(payload) != claim:
            raise RuntimeError("Riverhog did not abandon the expected processing claim")

    def retire_input(self, record: WorkRecord, collection_id: int) -> bool:
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
            return True
        blockers = plan.get("blockers")
        challenge = plan.get("challenge")
        if blockers:
            return False
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
        return True

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
        inputs: Iterable[Mapping[str, object]],
        purpose: str,
    ) -> ClaimBinding:
        payload = self.api.create_or_resume_processing_claim(
            work_id=identity,
            work_document=dict(document),
            work_document_sha256=riverhog_canonical_json_sha256(document),
            inputs=(dict(item) for item in inputs),
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
        actions: Sequence[CapabilityAction],
        artifacts: Iterable[CollectionArtifactIdentity],
    ) -> TransformCapabilityDocument:
        payload = self.api.create_transform_capability(
            claim.claim_id,
            fence=claim.fence,
            audience=audience,
            actions=tuple(actions),
            artifacts=(item.as_dict() for item in artifacts),
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


def _artifact_identity(
    value: ArtifactSubject | InputArtifact,
) -> CollectionArtifactIdentity:
    return CollectionArtifactIdentity(
        collection=CollectionRootIdentity(
            collection_id=value.collection.collection_id,
            archive_root_sha256=value.collection.archive_root_sha256,
            content_identity=value.collection.content_identity,
        ),
        path=value.path,
        bytes=value.bytes,
        sha256=value.sha256,
    )


def _target_binding(plan: TargetPlan) -> TargetPlanBinding:
    return TargetPlanBinding(
        protocol=plan.protocol,
        target_implementation_id=plan.target_implementation_id,
        target_contract_sha256=plan.target_contract_sha256,
        operation_contract_sha256=plan.operation_contract_sha256,
        plan=plan.binding_document(),
        plan_sha256=plan.plan_sha256,
    )


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


def _claim_binding(value: ProcessingClaimDocument | Mapping[str, Any]) -> ClaimBinding:
    return ClaimBinding(
        claim_id=_text(value.get("id"), "claim id"),
        fence=_positive_int(value.get("fence"), "claim fence"),
    )


def _token(value: TransformCapabilityDocument | Mapping[str, Any]) -> str:
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

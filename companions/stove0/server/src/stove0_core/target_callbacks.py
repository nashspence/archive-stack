"""Execution-scoped target callback capabilities owned by Stove0."""

from __future__ import annotations

import base64
import hmac
import json
import time
from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol

from riverhog_protocol.collection_workflow_transport import DISPOSITION_BATCH_MAX
from riverhog_protocol.collection_workflows import (
    ArtifactDisposition,
    ArtifactDispositionOutput,
    ArtifactDispositionSetIdentity,
)
from stove0_protocol import canonical_json_bytes
from stove0_target_protocol import (
    InputArtifact,
    InputDispositionDeclaration,
    OperationContract,
    OutputArtifact,
    OutputArtifactRoleCount,
    OutputArtifactSetIdentity,
    OutputSourceEdge,
    TargetCallbackAccess,
    TargetInputPage,
    TargetProductionAuthority,
    TargetProductionAuthorityPayload,
    TargetProductionSealResponse,
    update_input_disposition_commitment,
    update_output_artifact_commitment,
    update_output_source_edge_commitment,
)

from stove0_core._checkpoint_sha256 import CheckpointSHA256
from stove0_core.work_state import (
    ConcurrentWorkUpdate,
    TargetProductionSealCheckpoint,
    TargetProductionSealRecord,
    WorkRecord,
    WorkStore,
)

CallbackAction = Literal[
    "inputs:read",
    "outputs:declare",
    "dispositions:declare",
    "source-edges:declare",
    "production:seal",
]
_AUDIENCE = "stove0-target-callback/v1"


class OperationAuthority(Protocol):
    def operation_contract(self, operation: Any) -> OperationContract: ...


class ProductionProjector(Protocol):
    def project_target_dispositions(
        self,
        record: WorkRecord,
        dispositions: Sequence[ArtifactDisposition],
    ) -> None: ...

    def project_target_source_edges(
        self,
        record: WorkRecord,
        edges: Sequence[ArtifactDispositionOutput],
    ) -> None: ...

    def seal_target_projection(
        self,
        record: WorkRecord,
    ) -> ArtifactDispositionSetIdentity | None: ...


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)


class TargetCallbackAuthority:
    """Issue and verify opaque callback tokens without persisting bearer secrets."""

    def __init__(
        self,
        store: WorkStore,
        *,
        signing_key: str,
        base_url: str,
        allow_insecure_http: bool,
        ttl_seconds: int,
        operations: OperationAuthority | None = None,
        projector: ProductionProjector | None = None,
        seal_batch_size: int = 100,
    ) -> None:
        if len(signing_key.encode("utf-8")) < 16:
            raise ValueError("target callback signing key is too short")
        if ttl_seconds < 30:
            raise ValueError("target callback capability TTL must be at least 30 seconds")
        self.store = store
        self._key = hmac.digest(
            signing_key.encode("utf-8"),
            b"stove0-target-callback-signing-key/v1",
            "sha256",
        )
        self.base_url = base_url.rstrip("/")
        self.allow_insecure_http = allow_insecure_http
        self.ttl_seconds = ttl_seconds
        self.operations = operations
        self.projector = projector
        if seal_batch_size < 1 or seal_batch_size > DISPOSITION_BATCH_MAX:
            raise ValueError(
                f"target production seal batch size must be between 1 and {DISPOSITION_BATCH_MAX}"
            )
        self.seal_batch_size = seal_batch_size

    def issue_access(
        self,
        record: WorkRecord,
        target_registration_id: str,
    ) -> TargetCallbackAccess:
        job_id = _target_job_id(record)
        self.store.ensure_target_production_receiving(record.work_id, job_id)
        payload = self._payload(
            record,
            target_registration_id,
            actions=(
                "dispositions:declare",
                "inputs:read",
                "outputs:declare",
                "production:seal",
                "source-edges:declare",
            ),
        )
        encoded = _b64encode(canonical_json_bytes(payload))
        signature = _b64encode(hmac.digest(self._key, encoded.encode("ascii"), "sha256"))
        return TargetCallbackAccess(
            stove0_base_url=self.base_url,
            token=f"{encoded}.{signature}",
            allow_insecure_http=self.allow_insecure_http,
        )

    def input_page(
        self,
        token: str,
        *,
        job_id: str,
        continuation: str | None,
        limit: int,
    ) -> TargetInputPage:
        record, payload = self._authorize(token, action="inputs:read", job_id=job_id)
        plan = record.target_plan
        assert plan is not None
        authority = plan.inputs
        if payload["authority_id"] != authority.selection.selection_sha256:
            raise PermissionError("target callback authority changed")
        artifacts, next_continuation, complete = self.store.selection_artifact_page(
            authority.selection.selection_sha256,
            continuation=continuation,
            limit=limit,
        )
        return TargetInputPage(
            authority=authority,
            continuation=continuation,
            next_continuation=next_continuation,
            complete=complete,
            artifacts=tuple(
                InputArtifact(
                    id=item.id,
                    role=item.role,
                    collection=item.collection,
                    path=item.path,
                    bytes=item.bytes,
                    sha256=item.sha256,
                    media_type=item.media_type,
                )
                for item in artifacts
            ),
        )

    def declare_output(self, token: str, *, job_id: str, output: OutputArtifact) -> None:
        record, _ = self._authorize(token, action="outputs:declare", job_id=job_id)
        self.store.record_target_output(record.work_id, job_id, output)

    def declare_disposition(
        self,
        token: str,
        *,
        job_id: str,
        disposition: InputDispositionDeclaration,
    ) -> None:
        record, _ = self._authorize(token, action="dispositions:declare", job_id=job_id)
        self.store.record_target_disposition(record.work_id, job_id, disposition)

    def declare_source_edge(
        self,
        token: str,
        *,
        job_id: str,
        edge: OutputSourceEdge,
    ) -> None:
        record, _ = self._authorize(token, action="source-edges:declare", job_id=job_id)
        self.store.record_target_source_edge(record.work_id, job_id, edge)

    def seal_production(self, token: str, *, job_id: str) -> TargetProductionSealResponse:
        record, _ = self._authorize(token, action="production:seal", job_id=job_id)
        if self.operations is None or self.projector is None:
            raise RuntimeError("target production settlement is unavailable")
        if record.workflow_plan is None or record.target_plan is None:
            raise RuntimeError("target production has no sealed plan")
        seal = self.store.ensure_target_production_receiving(record.work_id, job_id)
        if seal.state == "receiving":
            seal = self._replace_seal(
                seal,
                state="sealing",
                checkpoint=TargetProductionSealCheckpoint(
                    output_hash_state=CheckpointSHA256().export_state(),
                    disposition_hash_state=CheckpointSHA256().export_state(),
                    source_edge_hash_state=CheckpointSHA256().export_state(),
                ),
            )
        if seal.state == "failed":
            raise ValueError(seal.failure or "target production sealing failed")
        if seal.state == "sealing":
            try:
                self._advance_seal(record.work_id, job_id)
            except ConcurrentWorkUpdate:
                # Another callback or scheduler worker durably advanced the same
                # immutable checkpoint. Re-read that authority instead of exposing
                # ordinary concurrent progress as a target failure.
                pass
            loaded = self.store.load_target_production_seal(record.work_id, job_id)
            if loaded is None:
                raise RuntimeError("target production seal disappeared")
            seal = loaded
        if seal.state == "failed":
            raise ValueError(seal.failure or "target production sealing failed")
        return TargetProductionSealResponse(
            state="sealed" if seal.state == "sealed" else "sealing",
            production=seal.production,
        )

    def process_due_production_seals(self, *, limit: int = 1) -> int:
        seals = self.store.scan_target_production_seals(state="sealing", limit=limit)
        for seal in seals:
            try:
                self._advance_seal(seal.work_id, seal.job_id)
            except ConcurrentWorkUpdate:
                continue
        return len(seals)

    def _advance_seal(self, work_id: str, job_id: str) -> None:
        seal = self.store.load_target_production_seal(work_id, job_id)
        record = self.store.load(work_id)
        if seal is None or seal.state != "sealing" or seal.checkpoint is None:
            return
        current_job = (
            record.controller_evidence.execution_envelope.execution_envelope_sha256
            if record is not None and record.controller_evidence is not None
            else None
        )
        if record is not None and current_job != job_id:
            self._replace_seal(
                seal,
                state="failed",
                checkpoint=None,
                failure="target execution generation was retired",
            )
            return
        if (
            record is None
            or record.workflow_plan is None
            or record.target_plan is None
            or record.controller_evidence is None
            or self.operations is None
            or self.projector is None
        ):
            raise RuntimeError("target production work authority is unavailable")
        operation = self.operations.operation_contract(record.workflow_plan.operation)
        try:
            checkpoint, production = self._advance_checkpoint(
                record,
                operation,
                seal.checkpoint,
            )
        except ValueError as exc:
            self._replace_seal(
                seal,
                state="failed",
                checkpoint=None,
                failure=str(exc)[:1000],
            )
            raise
        if production is not None:
            self._replace_seal(
                seal,
                state="sealed",
                checkpoint=None,
                production=production,
            )
        elif checkpoint != seal.checkpoint:
            self._replace_seal(seal, checkpoint=checkpoint)

    def _advance_checkpoint(
        self,
        record: WorkRecord,
        operation: OperationContract,
        checkpoint: TargetProductionSealCheckpoint,
    ) -> tuple[TargetProductionSealCheckpoint, TargetProductionAuthority | None]:
        assert record.target_plan is not None
        if checkpoint.phase == "outputs":
            return self._advance_outputs(record, operation, checkpoint), None
        if checkpoint.phase == "dispositions":
            return self._advance_dispositions(record, operation, checkpoint), None
        if checkpoint.phase == "source-edges":
            return self._advance_source_edges(record, operation, checkpoint), None
        if checkpoint.phase == "source-inputs":
            return self._advance_source_inputs(record, checkpoint), None
        if checkpoint.phase == "project-dispositions":
            return self._project_dispositions(record, checkpoint), None
        if checkpoint.phase == "project-source-edges":
            return self._project_source_edges(record, checkpoint), None
        if checkpoint.phase != "riverhog-seal":
            raise RuntimeError("target production seal has no current phase")
        assert self.projector is not None
        generic = self.projector.seal_target_projection(record)
        if generic is None:
            return checkpoint, None
        outputs = _output_identity(checkpoint)
        if (
            generic.disposition_count != checkpoint.disposition_count
            or generic.output_edge_count != checkpoint.source_edge_count
            or generic.output_artifact_count != checkpoint.output_count
        ):
            raise ValueError("Riverhog sealed a different generic derivation authority")
        if record.controller_evidence is None:
            raise RuntimeError("target production has no controller evidence")
        return checkpoint, TargetProductionAuthority.seal(
            TargetProductionAuthorityPayload(
                job_id=record.controller_evidence.execution_envelope.execution_envelope_sha256,
                plan_sha256=record.target_plan.plan_sha256,
                outputs=outputs,
                disposition_count=checkpoint.disposition_count,
                disposition_sha256=CheckpointSHA256.from_state(
                    checkpoint.disposition_hash_state
                ).hexdigest(),
                source_edge_count=checkpoint.source_edge_count,
                source_edge_sha256=CheckpointSHA256.from_state(
                    checkpoint.source_edge_hash_state
                ).hexdigest(),
                riverhog_disposition_set=generic,
            )
        )

    def _advance_outputs(
        self,
        record: WorkRecord,
        operation: OperationContract,
        checkpoint: TargetProductionSealCheckpoint,
    ) -> TargetProductionSealCheckpoint:
        page = self.store.target_output_page(
            record.work_id,
            _target_job_id(record),
            after_id=checkpoint.output_cursor,
            limit=self.seal_batch_size,
        )
        if not page:
            _validate_output_roles(_output_identity(checkpoint), operation)
            return checkpoint.model_copy(update={"phase": "dispositions"})
        digest = CheckpointSHA256.from_state(checkpoint.output_hash_state)
        counts = {item.role: item.count for item in checkpoint.output_roles}
        count = checkpoint.output_count
        for output in page:
            update_output_artifact_commitment(digest, ordinal=count, artifact=output)
            counts[output.role] = counts.get(output.role, 0) + 1
            count += 1
        return checkpoint.model_copy(
            update={
                "output_cursor": page[-1].id,
                "output_hash_state": digest.export_state(),
                "output_count": count,
                "output_bytes": checkpoint.output_bytes + sum(item.bytes for item in page),
                "output_roles": tuple(
                    OutputArtifactRoleCount(role=role, count=value)
                    for role, value in sorted(counts.items())
                ),
            }
        )

    def _advance_dispositions(
        self,
        record: WorkRecord,
        operation: OperationContract,
        checkpoint: TargetProductionSealCheckpoint,
    ) -> TargetProductionSealCheckpoint:
        assert record.target_plan is not None
        page = self.store.target_disposition_page(
            record.work_id,
            _target_job_id(record),
            after_id=checkpoint.disposition_cursor,
            limit=self.seal_batch_size,
        )
        if not page:
            if checkpoint.disposition_count != record.target_plan.inputs.selection.artifact_count:
                raise ValueError("target dispositions must cover the exact input authority")
            return checkpoint.model_copy(update={"phase": "source-edges"})
        contracts = {item.role: item for item in operation.inputs}
        digest = CheckpointSHA256.from_state(checkpoint.disposition_hash_state)
        count = checkpoint.disposition_count
        transformed = checkpoint.transformed_count
        for declaration in page:
            subject = self.store.load_selection_artifact(
                record.target_plan.inputs.selection.selection_sha256,
                declaration.input_id,
            )
            if subject is None or subject.role not in contracts:
                raise ValueError("target dispositions must cover the exact input authority")
            allowed = contracts[subject.role].allowed_dispositions
            if allowed is None or declaration.status not in allowed:
                raise ValueError(
                    f"target disposition is not permitted for input role: {subject.role}"
                )
            update_input_disposition_commitment(
                digest,
                ordinal=count,
                disposition=declaration,
            )
            count += 1
            transformed += int(declaration.status == "transformed")
        return checkpoint.model_copy(
            update={
                "disposition_cursor": page[-1].input_id,
                "disposition_hash_state": digest.export_state(),
                "disposition_count": count,
                "transformed_count": transformed,
            }
        )

    def _advance_source_edges(
        self,
        record: WorkRecord,
        operation: OperationContract,
        checkpoint: TargetProductionSealCheckpoint,
    ) -> TargetProductionSealCheckpoint:
        assert record.target_plan is not None
        page = self.store.target_source_edge_page(
            record.work_id,
            _target_job_id(record),
            order="output",
            after_output_id=checkpoint.source_edge_output_cursor,
            after_input_id=checkpoint.source_edge_input_cursor,
            limit=self.seal_batch_size,
        )
        if not page:
            if (
                checkpoint.source_edge_count == 0
                or checkpoint.source_output_count != checkpoint.output_count
            ):
                raise ValueError("every target output must have source-edge evidence")
            return checkpoint.model_copy(update={"phase": "source-inputs"})
        output_contracts = {item.role: item for item in operation.outputs}
        digest = CheckpointSHA256.from_state(checkpoint.source_edge_hash_state)
        count = checkpoint.source_edge_count
        output_count = checkpoint.source_output_count
        last_output = checkpoint.last_source_output_id
        for edge in page:
            job_id = _target_job_id(record)
            output = self.store.load_target_output(record.work_id, job_id, edge.output_id)
            source = self.store.load_selection_artifact(
                record.target_plan.inputs.selection.selection_sha256,
                edge.input_id,
            )
            disposition = self.store.load_target_disposition(record.work_id, job_id, edge.input_id)
            contract = output_contracts.get(output.role) if output is not None else None
            if output is None or source is None or disposition is None or contract is None:
                raise ValueError("target source edge references an undeclared authority member")
            if disposition.status != "transformed":
                raise ValueError("only transformed inputs may produce source edges")
            if source.role not in contract.derived_from_roles:
                raise ValueError("target source edge violates the output role contract")
            update_output_source_edge_commitment(digest, ordinal=count, edge=edge)
            count += 1
            if edge.output_id != last_output:
                output_count += 1
                last_output = edge.output_id
        return checkpoint.model_copy(
            update={
                "source_edge_output_cursor": page[-1].output_id,
                "source_edge_input_cursor": page[-1].input_id,
                "source_edge_hash_state": digest.export_state(),
                "source_edge_count": count,
                "source_output_count": output_count,
                "last_source_output_id": last_output,
            }
        )

    def _advance_source_inputs(
        self,
        record: WorkRecord,
        checkpoint: TargetProductionSealCheckpoint,
    ) -> TargetProductionSealCheckpoint:
        page = self.store.target_source_edge_page(
            record.work_id,
            _target_job_id(record),
            order="input",
            after_output_id=checkpoint.source_input_output_cursor,
            after_input_id=checkpoint.source_input_input_cursor,
            limit=self.seal_batch_size,
        )
        if not page:
            if checkpoint.source_input_count != checkpoint.transformed_count:
                raise ValueError("every transformed input must have source-edge evidence")
            return checkpoint.model_copy(update={"phase": "project-dispositions"})
        input_count = checkpoint.source_input_count
        last_input = checkpoint.last_source_input_id
        for edge in page:
            if edge.input_id != last_input:
                input_count += 1
                last_input = edge.input_id
        return checkpoint.model_copy(
            update={
                "source_input_output_cursor": page[-1].output_id,
                "source_input_input_cursor": page[-1].input_id,
                "source_input_count": input_count,
                "last_source_input_id": last_input,
            }
        )

    def _project_dispositions(
        self,
        record: WorkRecord,
        checkpoint: TargetProductionSealCheckpoint,
    ) -> TargetProductionSealCheckpoint:
        assert record.target_plan is not None and self.projector is not None
        page = self.store.target_disposition_page(
            record.work_id,
            _target_job_id(record),
            after_id=checkpoint.projected_disposition_cursor,
            limit=self.seal_batch_size,
        )
        if not page:
            return checkpoint.model_copy(update={"phase": "project-source-edges"})
        selection = record.target_plan.inputs.selection.selection_sha256
        projected: list[ArtifactDisposition] = []
        for declaration in page:
            subject = self.store.load_selection_artifact(selection, declaration.input_id)
            if subject is None:
                raise ValueError("target disposition input disappeared")
            projected.append(
                ArtifactDisposition(
                    input_collection_id=subject.collection.collection_id,
                    input_archive_root_sha256=subject.collection.archive_root_sha256,
                    input_path=subject.path,
                    status=declaration.status,
                )
            )
        self.projector.project_target_dispositions(record, projected)
        return checkpoint.model_copy(update={"projected_disposition_cursor": page[-1].input_id})

    def _project_source_edges(
        self,
        record: WorkRecord,
        checkpoint: TargetProductionSealCheckpoint,
    ) -> TargetProductionSealCheckpoint:
        assert record.target_plan is not None and self.projector is not None
        page = self.store.target_source_edge_page(
            record.work_id,
            _target_job_id(record),
            order="output",
            after_output_id=checkpoint.projected_source_output_cursor,
            after_input_id=checkpoint.projected_source_input_cursor,
            limit=self.seal_batch_size,
        )
        if not page:
            return checkpoint.model_copy(update={"phase": "riverhog-seal"})
        selection = record.target_plan.inputs.selection.selection_sha256
        projected: list[ArtifactDispositionOutput] = []
        for edge in page:
            subject = self.store.load_selection_artifact(selection, edge.input_id)
            output = self.store.load_target_output(
                record.work_id, _target_job_id(record), edge.output_id
            )
            if subject is None or output is None:
                raise ValueError("target source edge authority disappeared")
            projected.append(
                ArtifactDispositionOutput(
                    input_collection_id=subject.collection.collection_id,
                    input_archive_root_sha256=subject.collection.archive_root_sha256,
                    input_path=subject.path,
                    output_path=output.path,
                )
            )
        self.projector.project_target_source_edges(record, projected)
        return checkpoint.model_copy(
            update={
                "projected_source_output_cursor": page[-1].output_id,
                "projected_source_input_cursor": page[-1].input_id,
            }
        )

    def _replace_seal(
        self,
        seal: TargetProductionSealRecord,
        **updates: object,
    ) -> TargetProductionSealRecord:
        replacement = TargetProductionSealRecord.model_validate(
            seal.model_copy(update={**updates, "revision": seal.revision + 1}).model_dump(
                mode="python"
            )
        )
        return self.store.compare_and_swap_target_production_seal(
            seal.work_id,
            seal.job_id,
            expected_revision=seal.revision,
            replacement=replacement,
        )

    def _payload(
        self,
        record: WorkRecord,
        target_registration_id: str,
        *,
        actions: tuple[CallbackAction, ...],
    ) -> dict[str, object]:
        if (
            record.claim is None
            or record.workflow_plan is None
            or record.target_plan is None
            or record.controller_evidence is None
        ):
            raise RuntimeError("target callback capability requires a sealed execution")
        if record.workflow_plan.target_registration_id != target_registration_id:
            raise RuntimeError("target callback provider differs from the workflow plan")
        return {
            "format": "stove0-target-callback-capability/v1",
            "audience": _AUDIENCE,
            "provider": target_registration_id,
            "job_id": record.controller_evidence.execution_envelope.execution_envelope_sha256,
            "work_id": record.work_id,
            "claim_id": record.claim.claim_id,
            "fence": record.claim.fence,
            "authority_id": record.target_plan.inputs.selection.selection_sha256,
            "actions": list(actions),
            "expires_at": int(time.time()) + self.ttl_seconds,
        }

    def _authorize(
        self,
        token: str,
        *,
        action: CallbackAction,
        job_id: str,
    ) -> tuple[WorkRecord, Mapping[str, Any]]:
        encoded, separator, signature = token.partition(".")
        if not separator or not encoded or not signature:
            raise PermissionError("target callback capability is malformed")
        expected = _b64encode(hmac.digest(self._key, encoded.encode("ascii"), "sha256"))
        if not hmac.compare_digest(signature, expected):
            raise PermissionError("target callback capability is invalid")
        try:
            payload = json.loads(_b64decode(encoded))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise PermissionError("target callback capability is malformed") from exc
        if not isinstance(payload, dict):
            raise PermissionError("target callback capability is malformed")
        if (
            payload.get("format") != "stove0-target-callback-capability/v1"
            or payload.get("audience") != _AUDIENCE
            or payload.get("job_id") != job_id
            or action not in payload.get("actions", [])
            or not isinstance(payload.get("expires_at"), int)
            or int(payload["expires_at"]) < int(time.time())
        ):
            raise PermissionError("target callback capability is unavailable")
        work_id = str(payload.get("work_id") or "")
        record = self.store.load(work_id)
        if (
            record is None
            or record.claim is None
            or record.workflow_plan is None
            or record.target_plan is None
            or record.controller_evidence is None
            or record.phase not in {"queued", "executing", "output_finalizing", "verifying"}
            or record.claim.claim_id != payload.get("claim_id")
            or record.claim.fence != payload.get("fence")
            or record.workflow_plan.target_registration_id != payload.get("provider")
            or record.controller_evidence.execution_envelope.execution_envelope_sha256 != job_id
            or record.target_plan.inputs.selection.selection_sha256 != payload.get("authority_id")
        ):
            raise PermissionError("target callback capability is stale")
        return record, payload


def _validate_output_roles(
    outputs: OutputArtifactSetIdentity,
    operation: OperationContract,
) -> None:
    counts = {item.role: item.count for item in outputs.roles}
    contracts = {item.role: item for item in operation.outputs}
    if set(counts) - set(contracts):
        raise ValueError("target production contains an unsupported output role")
    for role, contract in contracts.items():
        count = counts.get(role, 0)
        if count < contract.minimum or (contract.maximum is not None and count > contract.maximum):
            raise ValueError(f"target output role cardinality is invalid: {role}")


def _target_job_id(record: WorkRecord) -> str:
    if record.controller_evidence is None:
        raise RuntimeError("target execution has no immutable job identity")
    return record.controller_evidence.execution_envelope.execution_envelope_sha256


def _output_identity(checkpoint: TargetProductionSealCheckpoint) -> OutputArtifactSetIdentity:
    return OutputArtifactSetIdentity(
        artifact_count=checkpoint.output_count,
        total_bytes=checkpoint.output_bytes,
        roles=checkpoint.output_roles,
        sha256=CheckpointSHA256.from_state(checkpoint.output_hash_state).hexdigest(),
    )


__all__ = ["TargetCallbackAuthority"]

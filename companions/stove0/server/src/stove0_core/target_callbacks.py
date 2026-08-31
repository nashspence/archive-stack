"""Execution-scoped target callback capabilities owned by Stove0."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Iterable, Mapping
from itertools import zip_longest
from typing import Any, Literal, Protocol

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
    OutputArtifactSetIdentity,
    OutputSourceEdge,
    TargetCallbackAccess,
    TargetInputPage,
    TargetProductionAuthority,
    TargetProductionAuthorityPayload,
    TargetProductionSealResponse,
    update_input_disposition_commitment,
    update_output_source_edge_commitment,
)

from stove0_core.work_state import WorkRecord, WorkStore

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
    def project_target_production(
        self,
        record: WorkRecord,
        dispositions: Iterable[ArtifactDisposition],
        edges: Iterable[ArtifactDispositionOutput],
    ) -> ArtifactDispositionSetIdentity: ...


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
    ) -> None:
        if len(signing_key.encode("utf-8")) < 16:
            raise ValueError("target callback signing key is too short")
        if ttl_seconds < 30:
            raise ValueError("target callback capability TTL must be at least 30 seconds")
        self.store = store
        self._key = hashlib.sha256(
            b"stove0-target-callback-signing-key/v1\x00" + signing_key.encode("utf-8")
        ).digest()
        self.base_url = base_url.rstrip("/")
        self.allow_insecure_http = allow_insecure_http
        self.ttl_seconds = ttl_seconds
        self.operations = operations
        self.projector = projector

    def issue_access(
        self,
        record: WorkRecord,
        target_registration_id: str,
    ) -> TargetCallbackAccess:
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
        self.store.record_target_output(record.work_id, output)

    def declare_disposition(
        self,
        token: str,
        *,
        job_id: str,
        disposition: InputDispositionDeclaration,
    ) -> None:
        record, _ = self._authorize(token, action="dispositions:declare", job_id=job_id)
        self.store.record_target_disposition(record.work_id, disposition)

    def declare_source_edge(
        self,
        token: str,
        *,
        job_id: str,
        edge: OutputSourceEdge,
    ) -> None:
        record, _ = self._authorize(token, action="source-edges:declare", job_id=job_id)
        self.store.record_target_source_edge(record.work_id, edge)

    def seal_production(self, token: str, *, job_id: str) -> TargetProductionSealResponse:
        record, _ = self._authorize(token, action="production:seal", job_id=job_id)
        if self.operations is None or self.projector is None:
            raise RuntimeError("target production settlement is unavailable")
        if record.workflow_plan is None or record.target_plan is None:
            raise RuntimeError("target production has no sealed plan")
        operation = self.operations.operation_contract(record.workflow_plan.operation)
        outputs = OutputArtifactSetIdentity.seal_iterable(
            self.store.iter_target_outputs(record.work_id)
        )
        _validate_output_roles(outputs, operation)
        disposition_count, disposition_sha256 = self._validate_dispositions(record, operation)
        source_edge_count, source_edge_sha256 = self._validate_edges(record, operation)
        generic = self.projector.project_target_production(
            record,
            self._generic_dispositions(record),
            self._generic_edges(record),
        )
        if (
            generic.disposition_count != disposition_count
            or generic.output_edge_count != source_edge_count
            or generic.output_artifact_count != outputs.artifact_count
        ):
            raise RuntimeError("Riverhog sealed a different generic derivation authority")
        production = TargetProductionAuthority.seal(
            TargetProductionAuthorityPayload(
                job_id=job_id,
                plan_sha256=record.target_plan.plan_sha256,
                outputs=outputs,
                disposition_count=disposition_count,
                disposition_sha256=disposition_sha256,
                source_edge_count=source_edge_count,
                source_edge_sha256=source_edge_sha256,
                riverhog_disposition_set=generic,
            )
        )
        return TargetProductionSealResponse(
            production=production,
        )

    def _validate_dispositions(
        self,
        record: WorkRecord,
        operation: OperationContract,
    ) -> tuple[int, str]:
        assert record.target_plan is not None
        inputs = self.store.iter_selection_artifacts(
            record.target_plan.inputs.selection.selection_sha256
        )
        declarations = self.store.iter_target_dispositions(record.work_id)
        contracts = {item.role: item for item in operation.inputs}
        digest = hashlib.sha256()
        count = 0
        for subject, declaration in zip_longest(inputs, declarations):
            if subject is None or declaration is None or subject.id != declaration.input_id:
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
        if count == 0:
            raise ValueError("target production has no input dispositions")
        return count, digest.hexdigest()

    def _validate_edges(
        self,
        record: WorkRecord,
        operation: OperationContract,
    ) -> tuple[int, str]:
        assert record.target_plan is not None
        output_contracts = {item.role: item for item in operation.outputs}
        digest = hashlib.sha256()
        count = 0
        last_output_id: str | None = None
        outputs_with_edges = 0
        for edge in self.store.iter_target_source_edges(record.work_id):
            output = self.store.load_target_output(record.work_id, edge.output_id)
            source = self.store.load_selection_artifact(
                record.target_plan.inputs.selection.selection_sha256,
                edge.input_id,
            )
            disposition = self.store.load_target_disposition(record.work_id, edge.input_id)
            if output is None or source is None or disposition is None:
                raise ValueError("target source edge references an undeclared authority member")
            if disposition.status != "transformed":
                raise ValueError("only transformed inputs may produce source edges")
            if source.role not in output_contracts[output.role].derived_from_roles:
                raise ValueError("target source edge violates the output role contract")
            update_output_source_edge_commitment(digest, ordinal=count, edge=edge)
            count += 1
            if edge.output_id != last_output_id:
                outputs_with_edges += 1
                last_output_id = edge.output_id
        output_count = sum(1 for _ in self.store.iter_target_outputs(record.work_id))
        if count == 0 or outputs_with_edges != output_count:
            raise ValueError("every target output must have source-edge evidence")
        transformed = (
            item.input_id
            for item in self.store.iter_target_dispositions(record.work_id)
            if item.status == "transformed"
        )
        edge_inputs = _unique_edge_inputs(
            self.store.iter_target_source_edges_by_input(record.work_id)
        )
        if any(left != right for left, right in zip_longest(transformed, edge_inputs)):
            raise ValueError("every transformed input must have source-edge evidence")
        return count, digest.hexdigest()

    def _generic_dispositions(self, record: WorkRecord) -> Iterable[ArtifactDisposition]:
        assert record.target_plan is not None
        selection_sha256 = record.target_plan.inputs.selection.selection_sha256
        for declaration in self.store.iter_target_dispositions(record.work_id):
            subject = self.store.load_selection_artifact(selection_sha256, declaration.input_id)
            if subject is None:
                raise RuntimeError("target disposition input disappeared")
            yield ArtifactDisposition(
                input_collection_id=subject.collection.collection_id,
                input_archive_root_sha256=subject.collection.archive_root_sha256,
                input_path=subject.path,
                status=declaration.status,
            )

    def _generic_edges(self, record: WorkRecord) -> Iterable[ArtifactDispositionOutput]:
        assert record.target_plan is not None
        selection_sha256 = record.target_plan.inputs.selection.selection_sha256
        for edge in self.store.iter_target_source_edges(record.work_id):
            subject = self.store.load_selection_artifact(selection_sha256, edge.input_id)
            output = self.store.load_target_output(record.work_id, edge.output_id)
            if subject is None or output is None:
                raise RuntimeError("target source edge authority disappeared")
            yield ArtifactDispositionOutput(
                input_collection_id=subject.collection.collection_id,
                input_archive_root_sha256=subject.collection.archive_root_sha256,
                input_path=subject.path,
                output_path=output.path,
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


def _unique_edge_inputs(edges: Iterable[OutputSourceEdge]) -> Iterable[str]:
    previous: str | None = None
    for edge in edges:
        if edge.input_id != previous:
            yield edge.input_id
            previous = edge.input_id


__all__ = ["TargetCallbackAuthority"]

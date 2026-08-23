"""Target-facing Riverhog data-plane runtime for the stove0 protocol."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Self, cast

from pydantic import JsonValue
from riverhog_api_client.producer import ProducerFile, ProducerInput, ProducerStream
from riverhog_protocol.collection_workflows import (
    ArtifactDisposition,
    OperationIdentity,
    RecipeIdentity,
)
from riverhog_transform_sdk import (
    ClaimedArtifact,
    ClaimedCollectionRuntime,
    ClaimedRetrieval,
    CollectionTransformRuntime,
    DerivedCollectionReceipt,
    DerivedCollectionSpec,
    TransformWorkspace,
)
from stove0_target_protocol import (
    EFFECT_TARGET_PROTOCOL,
    ExternalEffectReceipt,
    ExternalEffectReceiptPayload,
    InputArtifact,
    OperationContract,
    OutputArtifact,
    OutputCollectionRef,
    TargetExecutionEvidence,
    TargetJobRequest,
    TargetJobStatus,
    TargetProgress,
    validate_status_against_request,
)

from stove0_target_support.execution import TargetExecutionSession

CancellationCheck = Callable[[], None]


class TargetExecutionRuntime:
    """One target job bound to a sealed stove0 execution envelope."""

    def __init__(
        self,
        request: TargetJobRequest,
        runtime: ClaimedCollectionRuntime | CollectionTransformRuntime,
        *,
        session: TargetExecutionSession | None = None,
    ) -> None:
        self.request = request
        self.runtime = runtime
        self.session = session
        self._runtime_binding: Any = None
        self._workspaces: list[TransformWorkspace] = []
        self._completed = False

    @classmethod
    def from_request(
        cls,
        request: TargetJobRequest,
        *,
        cancellation_check: CancellationCheck | None = None,
        producer_version: str = "development",
        session: TargetExecutionSession | None = None,
    ) -> TargetExecutionRuntime:
        declaration = request.declaration
        evidence = declaration.controller_evidence
        workflow = evidence.execution_envelope.workflow_plan
        work = workflow.work
        authority = request.runtime
        runtime: ClaimedCollectionRuntime | CollectionTransformRuntime
        if workflow.result_kind == "external-effect":
            runtime = ClaimedCollectionRuntime.from_capability(
                base_url=authority.riverhog_base_url,
                capability_token=authority.capability_token,
                allow_insecure_http=authority.allow_insecure_http,
                inputs=work.root_identities(),
                claim_id=declaration.claim_id,
                fence=declaration.fence,
                execution_id=declaration.job_id,
                work_id=work.work_id,
                cancellation_check=cancellation_check,
                input_retrieval_policy=workflow.input_retrieval_policy,
            )
        else:
            spec = DerivedCollectionSpec(
                inputs=work.root_identities(),
                recipe=RecipeIdentity(
                    work.recipe.id,
                    work.recipe.revision,
                    work.recipe.sha256,
                ),
                operation=OperationIdentity(
                    workflow.operation.id,
                    workflow.operation.sha256,
                ),
                output_tags=workflow.output_tags,
            )
            runtime = CollectionTransformRuntime.from_capability(
                base_url=authority.riverhog_base_url,
                capability_token=authority.capability_token,
                allow_insecure_http=authority.allow_insecure_http,
                spec=spec,
                claim_id=declaration.claim_id,
                fence=declaration.fence,
                execution_id=declaration.job_id,
                work_id=work.work_id,
                controller_evidence=evidence.model_dump(
                    mode="json",
                    by_alias=True,
                    exclude_none=True,
                ),
                producer_app="stove0-worker",
                producer_version=producer_version,
                cancellation_check=cancellation_check,
                input_retrieval_policy=workflow.input_retrieval_policy,
            )
        return cls(request, runtime, session=session)

    def __enter__(self) -> Self:
        self.runtime.__enter__()
        if self.session is not None:
            self._runtime_binding = self.session.runtime_registry.bind(
                self.request.declaration.job_id,
                self.runtime,
            )
            self._runtime_binding.__enter__()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        failures: list[Exception] = []
        for workspace in reversed(self._workspaces):
            if not workspace.root.exists() and not workspace.root.is_symlink():
                continue
            try:
                workspace.release()
            except Exception as cleanup_exc:
                failures.append(cleanup_exc)
        if self._runtime_binding is not None:
            try:
                self._runtime_binding.__exit__(exc_type, exc, tb)
            except Exception as cleanup_exc:
                failures.append(cleanup_exc)
            self._runtime_binding = None
        try:
            self.runtime.__exit__(exc_type, exc, tb)
        except Exception as cleanup_exc:
            failures.append(cleanup_exc)
        if exc is None and failures and not self._completed:
            raise RuntimeError("target execution cleanup failed before completion") from failures[0]

    def refresh_capability(self, token: str) -> None:
        self.runtime.refresh_capability(token)

    @property
    def completed(self) -> bool:
        """Whether this attempt has produced its one exact successful result."""

        return self._completed

    def inputs(self) -> tuple[tuple[InputArtifact, ClaimedArtifact], ...]:
        inventory = {item.key: item for item in self.runtime.inventory()}
        resolved: list[tuple[InputArtifact, ClaimedArtifact]] = []
        for expected in self.request.declaration.plan.inputs:
            key = (expected.collection.collection_id, expected.path)
            actual = inventory.get(key)
            if (
                actual is None
                or actual.root != expected.collection.to_identity()
                or actual.bytes != expected.bytes
                or actual.sha256 != expected.sha256
            ):
                raise RuntimeError(
                    "target input is not the current claimed artifact: " + expected.id
                )
            resolved.append((expected, actual))
        return tuple(resolved)

    def prepare_inputs(
        self,
        inputs: Sequence[InputArtifact] | None = None,
        **kwargs: Any,
    ) -> ClaimedRetrieval:
        available = dict(self.inputs())
        selected = tuple(inputs or self.request.declaration.plan.inputs)
        artifacts: list[ClaimedArtifact] = []
        for item in selected:
            artifact = available.get(item)
            if artifact is None:
                raise ValueError(f"target input is not authorized: {item.id}")
            artifacts.append(artifact)
        return self.runtime.prepare_inputs(artifacts, **kwargs)

    def open_workspace(self, root: Path) -> TransformWorkspace:
        workspace = self.runtime.open_workspace(
            root,
            assurance=self.request.declaration.workspace_assurance,
        )
        self._workspaces.append(workspace)
        return workspace

    def publish(
        self,
        outputs: Mapping[str, ProducerInput],
        *,
        artifacts: Sequence[OutputArtifact],
        execution_sha256: str,
        dispositions: Sequence[ArtifactDisposition],
        source_context: Mapping[str, object] | None = None,
        **kwargs: Any,
    ) -> tuple[DerivedCollectionReceipt, OutputCollectionRef]:
        if not isinstance(self.runtime, CollectionTransformRuntime):
            raise RuntimeError("external-effect execution cannot publish a Riverhog collection")
        declared = tuple(artifacts)
        if not declared:
            raise ValueError("successful target execution requires output artifacts")
        if [item.id for item in declared] != sorted(item.id for item in declared):
            raise ValueError("target output artifacts must be ordered by ID")
        if set(outputs) != {item.id for item in declared}:
            raise ValueError("target output sources must match the declared output IDs")
        producer_inputs: list[ProducerInput] = []
        for artifact in declared:
            source = outputs[artifact.id]
            identity = _producer_identity(source)
            if identity != (artifact.path, artifact.bytes, artifact.sha256):
                raise ValueError(f"target output identity does not match its source: {artifact.id}")
            producer_inputs.append(source)
        receipt = self.runtime.publish(
            producer_inputs,
            execution_envelope_sha256=(
                self.request.declaration.controller_evidence.execution_envelope.execution_envelope_sha256
            ),
            execution_sha256=execution_sha256,
            dispositions=dispositions,
            source_context={
                **dict(source_context or {}),
                "target_plan_sha256": self.request.declaration.plan.plan_sha256,
                "target_request_sha256": self.request.request_sha256,
            },
            **kwargs,
        )
        self._completed = True
        return receipt, OutputCollectionRef(
            collection_id=receipt.collection_id,
            manifest_sha256=receipt.manifest_sha256,
            content_identity=receipt.content_identity,
            derivation_sha256=receipt.derivation.sha256,
        )

    def publish_success(
        self,
        outputs: Mapping[str, ProducerInput],
        *,
        artifacts: Sequence[OutputArtifact],
        operation: OperationContract,
        execution_sha256: str,
        dispositions: Sequence[ArtifactDisposition],
        attempt: int = 1,
        runtime_evidence: Mapping[str, object] | None = None,
        source_context: Mapping[str, object] | None = None,
        **kwargs: Any,
    ) -> TargetJobStatus:
        """Publish the one authorized output and build its verified terminal status."""

        declared = tuple(artifacts)
        receipt, output_collection = self.publish(
            outputs,
            artifacts=declared,
            execution_sha256=execution_sha256,
            dispositions=dispositions,
            source_context=source_context,
            **kwargs,
        )
        plan = self.request.declaration.plan
        status = TargetJobStatus(
            protocol=plan.protocol,
            job_id=self.request.declaration.job_id,
            state="succeeded",
            attempt=attempt,
            request_sha256=self.request.request_sha256,
            plan_sha256=plan.plan_sha256,
            progress=TargetProgress(
                phase="done",
                completed=len(declared),
                total=len(declared),
                unit="artifacts",
            ),
            outputs=declared,
            output_collection=output_collection,
            execution_evidence=TargetExecutionEvidence(
                target_contract_sha256=plan.target_contract_sha256,
                operation_contract_sha256=plan.operation_contract_sha256,
                plan_sha256=plan.plan_sha256,
                execution_sha256=execution_sha256,
                runtime=cast(dict[str, JsonValue], dict(runtime_evidence or {})),
            ),
            derivation=receipt.derivation.as_dict(),
        )
        validate_status_against_request(status, self.request, operation)
        if self.session is not None:
            self.session.record_completed(status)
        return status

    def effect_success(
        self,
        result: Mapping[str, JsonValue],
        *,
        operation: OperationContract,
        execution_sha256: str,
        attempt: int = 1,
        runtime_evidence: Mapping[str, object] | None = None,
    ) -> TargetJobStatus:
        """Seal one externally committed effect as a canonical terminal receipt."""

        plan = self.request.declaration.plan
        if plan.protocol != EFFECT_TARGET_PROTOCOL or operation.result_kind != "external-effect":
            raise ValueError("external-effect success requires an effect plan and operation")
        evidence = TargetExecutionEvidence(
            target_contract_sha256=plan.target_contract_sha256,
            operation_contract_sha256=plan.operation_contract_sha256,
            plan_sha256=plan.plan_sha256,
            execution_sha256=execution_sha256,
            runtime=cast(dict[str, JsonValue], dict(runtime_evidence or {})),
        )
        receipt = ExternalEffectReceipt.seal(
            ExternalEffectReceiptPayload(
                job_id=self.request.declaration.job_id,
                request_sha256=self.request.request_sha256,
                target_contract_sha256=plan.target_contract_sha256,
                operation_contract_sha256=plan.operation_contract_sha256,
                plan_sha256=plan.plan_sha256,
                execution_sha256=execution_sha256,
                result=dict(result),
            )
        )
        status = TargetJobStatus(
            protocol=plan.protocol,
            job_id=self.request.declaration.job_id,
            state="succeeded",
            attempt=attempt,
            request_sha256=self.request.request_sha256,
            plan_sha256=plan.plan_sha256,
            progress=TargetProgress(phase="done", completed=1, total=1, unit="effect"),
            execution_evidence=evidence,
            effect_receipt=receipt,
        )
        validate_status_against_request(status, self.request, operation)
        self._completed = True
        if self.session is not None:
            self.session.record_completed(status)
        return status


def _producer_identity(source: ProducerInput) -> tuple[str, int, str]:
    if isinstance(source, ProducerStream):
        return source.path, source.bytes, source.sha256
    if not isinstance(source, ProducerFile):
        raise TypeError("unsupported target output source")
    digest = hashlib.sha256()
    byte_count = 0
    before = source.source.stat()
    with source.source.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            byte_count += len(chunk)
            digest.update(chunk)
    after = source.source.stat()
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise RuntimeError(f"target output changed while identifying it: {source.path}")
    return source.path, byte_count, digest.hexdigest()


__all__ = ["CancellationCheck", "TargetExecutionRuntime"]

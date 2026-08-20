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
    ClaimedRetrieval,
    CollectionTransformRuntime,
    DerivedCollectionReceipt,
    DerivedCollectionSpec,
    TransformWorkspace,
)
from stove0_target_protocol import (
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

CancellationCheck = Callable[[], None]


class TargetExecutionRuntime:
    """One target job bound to a sealed stove0 execution envelope."""

    def __init__(
        self,
        request: TargetJobRequest,
        runtime: CollectionTransformRuntime,
    ) -> None:
        self.request = request
        self.runtime = runtime

    @classmethod
    def from_request(
        cls,
        request: TargetJobRequest,
        *,
        cancellation_check: CancellationCheck | None = None,
        producer_version: str = "development",
    ) -> TargetExecutionRuntime:
        declaration = request.declaration
        evidence = declaration.controller_evidence
        workflow = evidence.execution_envelope.workflow_plan
        work = workflow.work
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
        authority = request.runtime
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
        return cls(request, runtime)

    def __enter__(self) -> Self:
        self.runtime.__enter__()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.runtime.__exit__(exc_type, exc, tb)

    def refresh_capability(self, token: str) -> None:
        self.runtime.refresh_capability(token)

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
        return self.runtime.open_workspace(
            root,
            assurance=self.request.declaration.workspace_assurance,
        )

    def publish(
        self,
        outputs: Mapping[str, ProducerInput],
        *,
        artifacts: Sequence[OutputArtifact],
        execution_sha256: str,
        dispositions: Sequence[ArtifactDisposition],
        provenance_journals: Mapping[str, bytes] | None = None,
        source_context: Mapping[str, object] | None = None,
        **kwargs: Any,
    ) -> tuple[DerivedCollectionReceipt, OutputCollectionRef]:
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
            provenance_journals=provenance_journals,
            source_context={
                **dict(source_context or {}),
                "target_plan_sha256": self.request.declaration.plan.plan_sha256,
                "target_request_sha256": self.request.request_sha256,
            },
            **kwargs,
        )
        return receipt, OutputCollectionRef(
            collection_id=receipt.collection_id,
            manifest_sha256=receipt.manifest_sha256,
            content_etag=receipt.content_etag,
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
        provenance_journals: Mapping[str, bytes] | None = None,
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
            provenance_journals=provenance_journals,
            source_context=source_context,
            **kwargs,
        )
        plan = self.request.declaration.plan
        status = TargetJobStatus(
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

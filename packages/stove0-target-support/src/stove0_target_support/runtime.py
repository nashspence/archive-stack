"""Target-facing Riverhog data-plane runtime for the stove0 protocol."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Self, cast

from pydantic import JsonValue
from riverhog_api_client.producer import (
    ProducerArtifactCustody,
    ProducerArtifactIdentity,
    ProducerFile,
    ProducerInput,
)
from riverhog_protocol.collection_workflows import (
    OperationIdentity,
    RecipeIdentity,
)
from riverhog_transform_sdk import (
    ClaimedArtifact,
    ClaimedCollectionRuntime,
    ClaimedRetrieval,
    CollectionTransformRuntime,
    DerivedCollectionSpec,
    IncrementalDerivedCollectionWriter,
    TransformWorkspace,
)
from stove0_target_client import TargetCallbackClient
from stove0_target_protocol import (
    EFFECT_TARGET_PROTOCOL,
    ExternalEffectReceipt,
    ExternalEffectReceiptPayload,
    InputArtifact,
    InputDisposition,
    InputDispositionDeclaration,
    OperationContract,
    OutputArtifact,
    OutputCollectionRef,
    OutputSourceEdge,
    TargetExecutionEvidence,
    TargetJobRequest,
    TargetJobStatus,
    TargetProgress,
    validate_status_against_request,
)

from stove0_target_support.execution import TargetExecutionSession

CancellationCheck = Callable[[], None]


class TargetCollectionPublication:
    """Target-protocol view of one generic Riverhog incremental construction."""

    def __init__(
        self,
        execution: TargetExecutionRuntime,
        writer: IncrementalDerivedCollectionWriter,
    ) -> None:
        self.execution = execution
        self.writer = writer
        self._local_files: dict[str, Path] = {}

    def append(
        self,
        source: ProducerInput,
        artifact: OutputArtifact,
        *,
        derived_from: Iterable[str],
    ) -> tuple[ProducerArtifactCustody, ...]:
        if source.path != artifact.path:
            raise ValueError(f"target output path does not match its source: {artifact.id}")
        if isinstance(source, ProducerFile):
            self._local_files[artifact.path] = source.source
        runtime = cast(CollectionTransformRuntime, self.execution.runtime)
        receipts = runtime.append_incremental_output(
            self.writer,
            source,
            identity=ProducerArtifactIdentity(
                path=artifact.path,
                bytes=artifact.bytes,
                sha256=artifact.sha256,
            ),
        )
        self.execution._input_client.declare_target_execution_output(
            self.execution.job_id, artifact
        )
        source_count = 0
        for input_id in derived_from:
            self.execution._input_client.declare_target_execution_source_edge(
                self.execution.job_id,
                OutputSourceEdge(output_id=artifact.id, input_id=input_id),
            )
            source_count += 1
        if source_count == 0:
            raise ValueError("target output source references must be nonempty")
        self._release_custodied_files(receipts)
        return receipts

    def finish_success(
        self,
        *,
        operation: OperationContract,
        execution_sha256: str,
        attempt: int = 1,
        runtime_evidence: Mapping[str, object] | None = None,
        **kwargs: Any,
    ) -> TargetJobStatus:
        sealed = self.execution._input_client.seal_target_execution_production(
            self.execution.job_id
        )
        while sealed.state == "sealing":
            time.sleep(0.1)
            sealed = self.execution._input_client.seal_target_execution_production(
                self.execution.job_id
            )
        production = sealed.production
        if production is None:
            raise RuntimeError("Stove0 sealed no target production authority")
        disposition_set = production.riverhog_disposition_set
        runtime = cast(CollectionTransformRuntime, self.execution.runtime)
        receipt = runtime.finish_incremental_publication(
            self.writer,
            execution_sha256=execution_sha256,
            disposition_set=disposition_set,
            **kwargs,
        )
        self._release_all_files()
        output_collection = OutputCollectionRef(
            collection_id=receipt.collection_id,
            archive_root_sha256=receipt.archive_root_sha256,
            content_identity=receipt.content_identity,
            derivation_sha256=receipt.derivation.sha256,
        )
        plan = self.execution.request.declaration.plan
        status = TargetJobStatus(
            protocol=plan.protocol,
            job_id=self.execution.request.declaration.job_id,
            state="succeeded",
            attempt=attempt,
            request_sha256=self.execution.request.request_sha256,
            plan_sha256=plan.plan_sha256,
            progress=TargetProgress(
                phase="done",
                completed=production.outputs.artifact_count,
                total=production.outputs.artifact_count,
                unit="artifacts",
            ),
            production=production,
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
        validate_status_against_request(status, self.execution.request, operation)
        self.execution._completed = True
        if self.execution.session is not None:
            self.execution.session.record_completed(status)
        return status

    def _release_custodied_files(self, receipts: Iterable[ProducerArtifactCustody]) -> None:
        for receipt in receipts:
            local = self._local_files.pop(receipt.artifact.path, None)
            if local is None:
                continue
            try:
                local.unlink()
            except FileNotFoundError:
                pass

    def _release_all_files(self) -> None:
        for local in self._local_files.values():
            try:
                local.unlink()
            except FileNotFoundError:
                pass
        self._local_files.clear()


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
        self._input_client = TargetCallbackClient(request.callback_access)
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
        try:
            self._input_client.close()
        except Exception as cleanup_exc:
            failures.append(cleanup_exc)
        if exc is None and failures and not self._completed:
            raise RuntimeError("target execution cleanup failed before completion") from failures[0]

    def refresh_capability(self, token: str) -> None:
        self.runtime.refresh_capability(token)

    @property
    def job_id(self) -> str:
        return self.request.declaration.job_id

    @property
    def completed(self) -> bool:
        """Whether this attempt has produced its one exact successful result."""

        return self._completed

    def iter_inputs(self) -> Iterator[tuple[InputArtifact, ClaimedArtifact]]:
        for expected in self._input_client.iter_inputs(self.request.declaration.job_id):
            yield (
                expected,
                ClaimedArtifact(
                    root=expected.collection.to_identity(),
                    path=expected.path,
                    bytes=expected.bytes,
                    sha256=expected.sha256,
                    control=False,
                ),
            )

    def resolve_input_ids(self, input_ids: Sequence[str]) -> tuple[ClaimedArtifact, ...]:
        wanted = set(input_ids)
        if not wanted or len(wanted) != len(tuple(input_ids)):
            raise ValueError("target input references must be nonempty and unique")
        resolved: dict[str, ClaimedArtifact] = {}
        for expected, claimed in self.iter_inputs():
            if expected.id in wanted:
                resolved[expected.id] = claimed
                if len(resolved) == len(wanted):
                    break
        if set(resolved) != wanted:
            missing = sorted(wanted - set(resolved))[0]
            raise ValueError(f"target output references an unknown input: {missing}")
        return tuple(resolved[item] for item in sorted(resolved))

    def declare_disposition(self, input_id: str, status: InputDisposition) -> None:
        self._input_client.declare_target_execution_disposition(
            self.job_id,
            InputDispositionDeclaration(input_id=input_id, status=status),
        )

    def prepare_inputs(
        self,
        inputs: Sequence[InputArtifact] | None = None,
        **kwargs: Any,
    ) -> ClaimedRetrieval:
        if inputs is None:
            raise ValueError("target retrieval requires an explicit bounded input selection")
        artifacts = [
            ClaimedArtifact(
                root=item.collection.to_identity(),
                path=item.path,
                bytes=item.bytes,
                sha256=item.sha256,
                control=False,
            )
            for item in inputs
        ]
        return self.runtime.prepare_inputs(artifacts, **kwargs)

    def open_workspace(self, root: Path) -> TransformWorkspace:
        workspace = self.runtime.open_workspace(
            root,
            assurance=self.request.declaration.workspace_assurance,
        )
        self._workspaces.append(workspace)
        return workspace

    def open_collection_publication(
        self,
        *,
        source_context: Mapping[str, object] | None = None,
    ) -> TargetCollectionPublication:
        if not isinstance(self.runtime, CollectionTransformRuntime):
            raise RuntimeError("external-effect execution cannot publish a Riverhog collection")
        writer = self.runtime.open_incremental_publication(
            execution_envelope_sha256=(
                self.request.declaration.controller_evidence.execution_envelope.execution_envelope_sha256
            ),
            source_context={
                **dict(source_context or {}),
                "target_plan_sha256": self.request.declaration.plan.plan_sha256,
                "target_request_sha256": self.request.request_sha256,
            },
        )
        return TargetCollectionPublication(self, writer)

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


__all__ = ["CancellationCheck", "TargetCollectionPublication", "TargetExecutionRuntime"]

"""Shared rendering mechanics for exact review publication targets."""

from __future__ import annotations

import hashlib
import os
import threading
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from jsonschema import Draft202012Validator
from pydantic import JsonValue
from riverhog_api_client import ProducerFile
from riverhog_protocol import ArtifactDisposition, canonical_json_bytes, canonical_json_sha256
from riverhog_transform_sdk import TransformWorkspace
from stove0_protocol import JsonSchemaDocument
from stove0_review_sampler_client import ReviewSamplerClient
from stove0_review_sampler_protocol import (
    SamplerDescriptor,
    SamplerInput,
    SamplerRequest,
    SamplerRequestPayload,
    SamplerResult,
    SamplerWindow,
)
from stove0_review_target_contracts import (
    REVIEW_INDEX_ROLE,
    ReviewMaterializeIntent,
    validate_review_materialize_intent,
)
from stove0_target_support import (
    DEFAULT_TERMINAL_STATE_RETENTION_SECONDS,
    OperationContract,
    OutputArtifact,
    PersistentTargetService,
    TargetCollectionPublication,
    TargetContract,
    TargetContractPayload,
    TargetExecutionCanceled,
    TargetExecutionFailure,
    TargetExecutionInapplicable,
    TargetExecutionRuntime,
    TargetExecutionSession,
    TargetJobRequest,
    TargetJobStatus,
    TargetOperationSupport,
    TargetPreflightRequest,
    TargetPreflightResponse,
    TargetProtocol,
    TargetServiceError,
)

_SAMPLER_OPTION_PROPERTIES: dict[str, JsonValue] = {
    "sampler_registration_id": {
        "type": "string",
        "pattern": "^[a-z0-9](?:[a-z0-9._-]{0,118}[a-z0-9])?$",
    },
    "sampler_timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 86400},
    "maximum_output_bytes": {
        "type": "integer",
        "minimum": 1,
        "maximum": 1024**4,
    },
    "sampler_descriptor_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    "sampler_image_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
}


def review_options_schema(
    schema_id: str,
    *,
    required: tuple[str, ...] = (),
    properties: Mapping[str, JsonValue] | None = None,
) -> JsonSchemaDocument:
    """Build one exact target-owned schema over shared sampler selection options."""

    return JsonSchemaDocument.from_schema(
        schema_id,
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "required": ["sampler_registration_id", *required],
            "properties": {**_SAMPLER_OPTION_PROPERTIES, **dict(properties or {})},
            "additionalProperties": False,
        },
    )


@dataclass(frozen=True, slots=True)
class SamplerRegistration:
    id: str
    client: ReviewSamplerClient
    descriptor_sha256: str
    image_digest: str

    def descriptor(self) -> SamplerDescriptor:
        descriptor = self.client.descriptor()
        if descriptor.descriptor_sha256 != self.descriptor_sha256:
            raise RuntimeError(f"configured sampler descriptor changed: {self.id}")
        if descriptor.image_digest != self.image_digest:
            raise RuntimeError(f"configured sampler image digest changed: {self.id}")
        return descriptor


class ReviewTargetServiceBase(PersistentTargetService, ABC):
    """Common sampler orchestration with publication owned by an exact target."""

    def __init__(
        self,
        *,
        state_root: Path,
        workspace_root: Path,
        samplers: tuple[SamplerRegistration, ...],
        source_revision: str = "unknown",
        image_digest: str,
        implementation_version: str,
        protocol: TargetProtocol,
        implementation_id: str,
        operation: OperationContract,
        options_schema: JsonSchemaDocument,
        terminal_state_retention_seconds: int = DEFAULT_TERMINAL_STATE_RETENTION_SECONDS,
    ) -> None:
        if not samplers or [item.id for item in samplers] != sorted(item.id for item in samplers):
            raise ValueError("review sampler registrations must be nonempty and ordered")
        if len({item.id for item in samplers}) != len(samplers):
            raise ValueError("review sampler registrations must be unique")
        self.workspace_root = workspace_root.resolve()
        self.workspace_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.workspace_root, 0o700)
        self.samplers = {item.id: item for item in samplers}
        self.image_digest = image_digest
        self.implementation_version = implementation_version
        self.operation = operation
        contract = TargetContract.seal(
            TargetContractPayload(
                protocol=protocol,
                implementation_id=implementation_id,
                implementation_version=implementation_version,
                source_revision=source_revision,
                image_digest=image_digest,
                operations=(
                    TargetOperationSupport(
                        operation_id=operation.id,
                        operation_contract_sha256=operation.contract_sha256,
                        result_kind=operation.result_kind,
                        options_schema=options_schema,
                    ),
                ),
            )
        )
        super().__init__(
            contract=contract,
            operations={operation.id: operation},
            state_root=state_root,
            execute=self._execute,
            intent_semantic_validators={
                operation.intent_semantics.profile_sha256: validate_review_materialize_intent
            },
            terminal_state_retention_seconds=terminal_state_retention_seconds,
        )

    def preflight(self, request: TargetPreflightRequest) -> TargetPreflightResponse:
        try:
            intent = ReviewMaterializeIntent.model_validate(request.intent)
        except ValueError as exc:
            raise TargetServiceError(
                400,
                "invalid_target_request",
                "review target intent is invalid",
            ) from exc
        try:
            sampler_id = str(request.target_options["sampler_registration_id"])
        except (KeyError, ValueError) as exc:
            raise TargetServiceError(
                400,
                "invalid_target_request",
                "review target requires a sampler registration",
            ) from exc
        try:
            registration = self.samplers[sampler_id]
        except KeyError as exc:
            raise TargetServiceError(
                400,
                "invalid_target_request",
                f"review sampler is not configured: {sampler_id}",
            ) from exc
        descriptor = registration.descriptor()
        try:
            Draft202012Validator(descriptor.portable_intent_schema.document).validate(
                intent.variant.portable_intent
            )
        except Exception as exc:
            raise TargetServiceError(
                400,
                "invalid_target_request",
                "review variant intent is invalid for the selected sampler",
            ) from exc
        expected: dict[str, JsonValue] = {
            "sampler_descriptor_sha256": descriptor.descriptor_sha256,
            "sampler_image_digest": descriptor.image_digest,
            **self._fixed_target_options(),
        }
        for key, value in expected.items():
            supplied = request.target_options.get(key)
            if supplied is not None and supplied != value:
                raise TargetServiceError(
                    400,
                    "invalid_target_request",
                    f"configured review {key} differs from the requested value",
                )
        effective = request.model_copy(
            update={"target_options": {**request.target_options, **expected}}
        )
        return super().preflight(effective)

    def close(self) -> None:
        super().close()
        for registration in self.samplers.values():
            registration.client.close()

    def readiness(self) -> dict[str, str]:
        return {
            registration.id: registration.descriptor().descriptor_sha256
            for registration in self.samplers.values()
        }

    def _fixed_target_options(self) -> dict[str, JsonValue]:
        return {}

    def _validate_sealed_target_options(self, options: Mapping[str, JsonValue]) -> None:
        for key, value in self._fixed_target_options().items():
            if options.get(key) != value:
                raise RuntimeError(f"sealed review plan differs from configured {key}")

    @abstractmethod
    def _open_collection_publication(
        self,
        execution: TargetExecutionRuntime,
    ) -> TargetCollectionPublication | None:
        """Open the exact publication mechanism owned by this target."""

    @abstractmethod
    def _finish_publication(
        self,
        *,
        execution: TargetExecutionRuntime,
        workspace: TransformWorkspace,
        request: TargetJobRequest,
        publication: TargetCollectionPublication | None,
        artifacts: tuple[OutputArtifact, ...],
        dispositions: tuple[ArtifactDisposition, ...],
        execution_sha256: str,
        attempt: int,
        runtime_evidence: dict[str, JsonValue],
    ) -> TargetJobStatus:
        """Publish the exact result shape owned by this target."""

    def _execute(
        self,
        request: TargetJobRequest,
        attempt: int,
        cancellation: threading.Event,
        session: TargetExecutionSession,
    ) -> TargetJobStatus:
        intent = ReviewMaterializeIntent.model_validate(request.declaration.plan.intent)
        sample_plan = intent.sample_plan
        portable_intent = intent.variant.portable_intent
        variant_id = intent.variant.id
        options = request.declaration.plan.target_options
        self._validate_sealed_target_options(options)
        sampler_id = str(options["sampler_registration_id"])
        try:
            registration = self.samplers[sampler_id]
        except KeyError as exc:
            raise TargetExecutionInapplicable(
                "sampler-not-configured", f"Review sampler is not configured: {sampler_id}"
            ) from exc
        descriptor = registration.descriptor()
        if (
            options.get("sampler_descriptor_sha256") != descriptor.descriptor_sha256
            or options.get("sampler_image_digest") != descriptor.image_digest
        ):
            raise RuntimeError("sealed review plan differs from the selected sampler")
        Draft202012Validator(descriptor.portable_intent_schema.document).validate(portable_intent)
        timeout = options.get("sampler_timeout_seconds", 86400)
        maximum = options.get("maximum_output_bytes", 8 * 1024**3)
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            raise ValueError("sampler_timeout_seconds must be an integer")
        if isinstance(maximum, bool) or not isinstance(maximum, int):
            raise ValueError("maximum_output_bytes must be an integer")

        def check() -> None:
            if cancellation.is_set():
                raise TargetExecutionCanceled("review target was canceled")

        with TargetExecutionRuntime.from_request(
            request,
            cancellation_check=check,
            producer_version=self.implementation_version,
            session=session,
        ) as execution:
            workspace = execution.open_workspace(self.workspace_root)
            try:
                resolved = execution.inputs()
                windows = tuple(
                    SamplerWindow(
                        id=f"sample-{index:04d}",
                        input_id=window.artifact_id,
                        start_ms=window.start_ms,
                        duration_ms=window.duration_ms,
                        output_path=f"output/review/samples/{index:04d}-{window.artifact_id}{_suffix(descriptor)}",
                    )
                    for index, window in enumerate(sample_plan.windows, start=1)
                )
                artifacts: list[OutputArtifact] = []
                publication = self._open_collection_publication(execution)
                samples: list[dict[str, object]] = []
                sampler_requests: list[SamplerRequest] = []
                sampler_results: list[SamplerResult] = []
                allowed_output_paths: set[str] = set()
                produced_bytes = 0
                for artifact, claimed in sorted(resolved, key=lambda item: item[0].id):
                    artifact_windows = tuple(
                        item for item in windows if item.input_id == artifact.id
                    )
                    if not artifact_windows:
                        continue
                    check()
                    suffix = PurePosixPath(artifact.path).suffix[:32]
                    relative = f"input/{artifact.id}{suffix}"
                    source = workspace.resolve(relative)
                    source.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    with execution.prepare_inputs((artifact,)) as retrieval:
                        retrieval.download(claimed, source)
                    try:
                        remaining_bytes = maximum - produced_bytes
                        if remaining_bytes < 1:
                            raise RuntimeError("review output exceeded its sealed byte budget")
                        sampler_request = SamplerRequest.seal(
                            SamplerRequestPayload(
                                sampler_descriptor_sha256=descriptor.descriptor_sha256,
                                workspace_id=workspace.execution_id,
                                inputs=(
                                    SamplerInput(
                                        id=artifact.id,
                                        path=relative,
                                        bytes=artifact.bytes,
                                        sha256=artifact.sha256,
                                        media_type=artifact.media_type,
                                    ),
                                ),
                                windows=artifact_windows,
                                portable_intent=portable_intent,
                                maximum_output_bytes=remaining_bytes,
                                timeout_seconds=timeout,
                                cancellation_path="control/cancel",
                            )
                        )
                        sampler_result = self._sample(
                            registration,
                            sampler_request,
                            cancellation=cancellation,
                            workspace=workspace,
                        )
                        _require_sampler_success(sampler_result)
                        sampler_requests.append(sampler_request)
                        sampler_results.append(sampler_result)
                        current_paths = {output.path for output in sampler_result.outputs}
                        allowed_output_paths.update(current_paths)
                        _verify_output_set(
                            workspace,
                            allowed=allowed_output_paths,
                            required=current_paths,
                        )
                        produced_bytes += sum(output.bytes for output in sampler_result.outputs)
                        for output in sorted(sampler_result.outputs, key=lambda item: item.path):
                            path = workspace.resolve(output.path)
                            _verify_file(path, output.bytes, output.sha256)
                            collection_path = output.path.removeprefix("output/")
                            output_artifact = OutputArtifact(
                                id=output.id,
                                role=descriptor.output_role,
                                path=collection_path,
                                bytes=output.bytes,
                                sha256=output.sha256,
                                media_type=output.media_type,
                                derived_from=output.derived_from,
                            )
                            artifacts.append(output_artifact)
                            if publication is not None:
                                publication.append(
                                    ProducerFile(path, collection_path), output_artifact
                                )
                            window = next(item for item in windows if item.id == output.id)
                            samples.append(
                                {
                                    "artifact_id": output.id,
                                    "source_artifact_id": window.input_id,
                                    "path": collection_path,
                                    "start_ms": window.start_ms,
                                    "duration_ms": window.duration_ms,
                                }
                            )
                    finally:
                        source.unlink(missing_ok=True)
                if not sampler_results:
                    raise RuntimeError("review sample plan produced no executable sampler groups")
                sampler_result_sha256 = canonical_json_sha256(
                    {
                        "format": "stove0-review-sampler-result-set/v1",
                        "results": [item.result_sha256 for item in sampler_results],
                    }
                )
                index_path = workspace.resolve("output/review/summary.json")
                index_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                index_path.write_bytes(
                    canonical_json_bytes(
                        {
                            "format": "stove0-review-index/v1",
                            "variant_id": variant_id,
                            "sample_plan": sample_plan.model_dump(mode="json"),
                            "sampler_descriptor": descriptor.model_dump(mode="json"),
                            "sampler_request_sha256s": [
                                item.request_sha256 for item in sampler_requests
                            ],
                            "sampler_result_sha256s": [
                                item.result_sha256 for item in sampler_results
                            ],
                            "sampler_result_set_sha256": sampler_result_sha256,
                            "samples": samples,
                        }
                    )
                )
                index_bytes, index_sha = file_identity(index_path)
                index = OutputArtifact(
                    id="review-index",
                    role=REVIEW_INDEX_ROLE,
                    path="review/summary.json",
                    bytes=index_bytes,
                    sha256=index_sha,
                    media_type="application/json",
                    derived_from=tuple(sorted(item.id for item, _claimed in resolved)),
                )
                artifacts.append(index)
                if publication is not None:
                    publication.append(ProducerFile(index_path, index.path), index)
                declared = tuple(sorted(artifacts, key=lambda item: item.id))
                dispositions = tuple(
                    ArtifactDisposition(
                        input_collection_id=artifact.collection.collection_id,
                        input_archive_root_sha256=artifact.collection.archive_root_sha256,
                        input_path=artifact.path,
                        status="transformed",
                        outputs=tuple(
                            output.path for output in declared if artifact.id in output.derived_from
                        ),
                    )
                    for artifact, _claimed in resolved
                )
                execution_sha256 = _execution_sha256(
                    request.declaration.plan.plan_sha256,
                    self.image_digest,
                    sampler_result_sha256,
                    declared,
                )
                return self._finish_publication(
                    execution=execution,
                    workspace=workspace,
                    request=request,
                    publication=publication,
                    artifacts=declared,
                    dispositions=dispositions,
                    execution_sha256=execution_sha256,
                    attempt=attempt,
                    runtime_evidence={
                        "image_digest": self.image_digest,
                        "sampler_descriptor_sha256": descriptor.descriptor_sha256,
                        "sampler_image_digest": descriptor.image_digest,
                        "sampler_result_set_sha256": sampler_result_sha256,
                    },
                )
            finally:
                if not execution.completed:
                    workspace.release()

    @staticmethod
    def _sample(
        registration: SamplerRegistration,
        request: SamplerRequest,
        *,
        cancellation: threading.Event,
        workspace: TransformWorkspace,
    ) -> SamplerResult:
        stopped = threading.Event()

        def watch() -> None:
            while not stopped.wait(0.1):
                if cancellation.is_set():
                    path = workspace.resolve(request.cancellation_path)
                    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    path.write_bytes(b"stove0-review-cancel/v1\n")
                    return

        watcher = threading.Thread(target=watch, daemon=True, name="review-sampler-cancel")
        watcher.start()
        try:
            return registration.client.sample(request)
        finally:
            stopped.set()


def _suffix(descriptor: SamplerDescriptor) -> str:
    if descriptor.output_role.endswith("audio/v1"):
        return ".opus"
    if descriptor.output_role.endswith("video/v1"):
        return ".mkv"
    raise ValueError("sampler descriptor has an unsupported review output role")


def _require_sampler_success(result: SamplerResult) -> None:
    if result.state == "succeeded":
        return
    if result.state == "canceled":
        raise TargetExecutionCanceled("review sampler was canceled")
    if result.state == "inapplicable":
        outcome = result.inapplicable
        if outcome is None:
            raise RuntimeError("review sampler omitted its inapplicable outcome")
        raise TargetExecutionInapplicable(outcome.code, outcome.message)
    failure = result.failure
    if failure is None:
        raise RuntimeError("review sampler omitted its failure outcome")
    raise TargetExecutionFailure(
        failure.code,
        failure.message,
        retryable=failure.retryable,
    )


def _execution_sha256(
    plan_sha256: str,
    image_digest: str,
    sampler_result_sha256: str,
    outputs: Sequence[OutputArtifact],
) -> str:
    """Identify exact review execution semantics independently of an attempt."""

    return canonical_json_sha256(
        {
            "format": "stove0-review-target-execution/v1",
            "plan_sha256": plan_sha256,
            "image_digest": image_digest,
            "sampler_result_sha256": sampler_result_sha256,
            "outputs": [item.model_dump(mode="json") for item in outputs],
        }
    )


def file_identity(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(8 * 1024**2):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _verify_file(path: Path, expected_bytes: int, expected_sha256: str) -> None:
    size, sha256 = file_identity(path)
    if (size, sha256) != (expected_bytes, expected_sha256):
        raise RuntimeError("sampler output differs from its declared identity")


def _verify_output_set(
    workspace: TransformWorkspace,
    *,
    allowed: set[str],
    required: set[str],
) -> None:
    output_root = workspace.resolve("output")
    actual = {
        path.relative_to(workspace.root).as_posix()
        for path in output_root.rglob("*")
        if path.is_file()
    }
    if not required <= actual or not actual <= allowed:
        raise RuntimeError("sampler workspace contains an undeclared output")


__all__ = [
    "ReviewTargetServiceBase",
    "SamplerRegistration",
    "file_identity",
    "review_options_schema",
]

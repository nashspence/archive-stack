"""Review collection authority that delegates only terminal sample rendering."""

from __future__ import annotations

import hashlib
import importlib.metadata
import os
import subprocess
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

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
    REVIEW_MATERIALIZE_OPERATION,
    REVIEW_RCLONE_DELIVER_OPERATION,
    ReviewSamplePlan,
)
from stove0_target_support import (
    DEFAULT_TERMINAL_STATE_RETENTION_SECONDS,
    OutputArtifact,
    PersistentTargetService,
    TargetContract,
    TargetContractPayload,
    TargetEffectCommitUncertain,
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

COLLECTION_OPTIONS = JsonSchemaDocument.from_schema(
    "stove0.review-target-options/v1",
    {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["sampler_registration_id"],
        "properties": _SAMPLER_OPTION_PROPERTIES,
        "additionalProperties": False,
    },
)

EFFECT_OPTIONS = JsonSchemaDocument.from_schema(
    "stove0.review-rclone-target-options/v1",
    {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["sampler_registration_id", "destination_identity"],
        "properties": {
            **_SAMPLER_OPTION_PROPERTIES,
            "destination_identity": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        },
        "additionalProperties": False,
    },
)


def _version() -> str:
    try:
        return importlib.metadata.version("stove0-review-target")
    except importlib.metadata.PackageNotFoundError:
        return "development"


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


@dataclass(frozen=True, slots=True)
class RcloneReviewDestination:
    """Deployment-owned rclone destination for one fixed effect target."""

    identity: str
    remote: str
    config_path: Path | None = None
    executable: str = "rclone"
    timeout_seconds: int = 86400

    def __post_init__(self) -> None:
        if len(self.identity) != 64 or any(
            value not in "0123456789abcdef" for value in self.identity
        ):
            raise ValueError("review destination identity must be a lowercase SHA-256")
        if not self.remote or self.remote != self.remote.strip():
            raise ValueError("review rclone destination must be nonempty and canonical")
        if not self.executable or self.executable != self.executable.strip():
            raise ValueError("review rclone executable must be nonempty and canonical")
        if self.config_path is not None and not self.config_path.is_absolute():
            raise ValueError("review rclone config path must be absolute")
        if self.timeout_seconds < 1:
            raise ValueError("review rclone timeout must be positive")

    def commit(
        self,
        *,
        delivery_id: str,
        output_root: Path,
        artifacts: Sequence[OutputArtifact],
        manifest_path: Path,
    ) -> dict[str, JsonValue]:
        """Commit exact review objects, then publish their manifest marker last."""

        destination = f"{self.remote.rstrip('/')}/{delivery_id}"
        common = [self.executable]
        if self.config_path is not None:
            common.extend(("--config", str(self.config_path)))
        try:
            subprocess.run(
                [*common, "copy", str(output_root), f"{destination}/objects"],
                check=True,
                capture_output=True,
                timeout=self.timeout_seconds,
            )
            subprocess.run(
                [*common, "copyto", str(manifest_path), f"{destination}/manifest.json"],
                check=True,
                capture_output=True,
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError:
            raise
        except Exception as exc:
            raise TargetEffectCommitUncertain(
                "review delivery may have committed; inspect the configured destination"
            ) from exc
        manifest_bytes, archive_root_sha256 = _identity(manifest_path)
        del manifest_bytes
        return {
            "format": "stove0-review-rclone-receipt/v1",
            "destination_identity": self.identity,
            "delivery_id": delivery_id,
            "artifact_archive_root_sha256": archive_root_sha256,
            "artifact_count": len(artifacts),
            "total_bytes": sum(item.bytes for item in artifacts),
        }


class ReviewTargetService(PersistentTargetService):
    def __init__(
        self,
        *,
        state_root: Path,
        workspace_root: Path,
        samplers: tuple[SamplerRegistration, ...],
        source_revision: str = "unknown",
        image_digest: str,
        mode: Literal["collection", "rclone-effect"] = "collection",
        destination: RcloneReviewDestination | None = None,
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
        self.mode = mode
        self.destination = destination
        if (mode == "rclone-effect") != (destination is not None):
            raise ValueError("rclone-effect review mode requires exactly one destination")
        operation = (
            REVIEW_RCLONE_DELIVER_OPERATION
            if mode == "rclone-effect"
            else REVIEW_MATERIALIZE_OPERATION
        )
        protocol: TargetProtocol = (
            "stove0-effect-target/v1" if mode == "rclone-effect" else "stove0-transform-target/v1"
        )
        contract = TargetContract.seal(
            TargetContractPayload(
                protocol=protocol,
                implementation_id=(
                    "stove0.review-rclone-effect-target/v1"
                    if mode == "rclone-effect"
                    else "stove0.review-collection-target/v1"
                ),
                implementation_version=_version(),
                source_revision=source_revision,
                image_digest=image_digest,
                operations=(
                    TargetOperationSupport(
                        operation_id=operation.id,
                        operation_contract_sha256=operation.contract_sha256,
                        result_kind=operation.result_kind,
                        options_schema=(
                            EFFECT_OPTIONS if mode == "rclone-effect" else COLLECTION_OPTIONS
                        ),
                    ),
                ),
            )
        )
        super().__init__(
            contract=contract,
            operations={operation.id: operation},
            state_root=state_root,
            execute=self._execute,
            terminal_state_retention_seconds=terminal_state_retention_seconds,
        )

    def preflight(self, request: TargetPreflightRequest) -> TargetPreflightResponse:
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
        expected = {
            "sampler_descriptor_sha256": descriptor.descriptor_sha256,
            "sampler_image_digest": descriptor.image_digest,
        }
        if self.destination is not None:
            expected["destination_identity"] = self.destination.identity
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

    def _execute(
        self,
        request: TargetJobRequest,
        attempt: int,
        cancellation: threading.Event,
        session: TargetExecutionSession,
    ) -> TargetJobStatus:
        intent = request.declaration.plan.intent
        raw_plan = intent.get("sample_plan")
        raw_variant = intent.get("variant")
        if not isinstance(raw_plan, dict) or not isinstance(raw_variant, dict):
            raise ValueError("review intent has no sample plan or variant")
        sample_plan = ReviewSamplePlan.model_validate(raw_plan)
        portable_intent = raw_variant.get("portable_intent")
        variant_id = raw_variant.get("id")
        if not isinstance(portable_intent, dict) or not isinstance(variant_id, str):
            raise ValueError("review variant intent is invalid")
        options = request.declaration.plan.target_options
        if self.destination is not None and (
            options.get("destination_identity") != self.destination.identity
        ):
            raise RuntimeError("sealed review plan differs from the configured destination")
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
            producer_version=_version(),
            session=session,
        ) as execution:
            workspace = execution.open_workspace(self.workspace_root)
            try:
                resolved = execution.inputs()
                inputs: list[SamplerInput] = []
                with execution.prepare_inputs(
                    tuple(item for item, _claimed in resolved)
                ) as retrieval:
                    for artifact, claimed in resolved:
                        check()
                        suffix = PurePosixPath(artifact.path).suffix[:32]
                        relative = f"input/{artifact.id}{suffix}"
                        source = workspace.resolve(relative)
                        source.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        retrieval.download(claimed, source)
                        inputs.append(
                            SamplerInput(
                                id=artifact.id,
                                path=relative,
                                bytes=artifact.bytes,
                                sha256=artifact.sha256,
                                media_type=artifact.media_type,
                            )
                        )
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
                sampler_request = SamplerRequest.seal(
                    SamplerRequestPayload(
                        sampler_descriptor_sha256=descriptor.descriptor_sha256,
                        workspace_id=workspace.execution_id,
                        inputs=tuple(sorted(inputs, key=lambda item: item.id)),
                        windows=windows,
                        portable_intent=portable_intent,
                        maximum_output_bytes=maximum,
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
                artifacts: list[OutputArtifact] = []
                producers: dict[str, ProducerFile] = {}
                samples: list[dict[str, object]] = []
                for output in sampler_result.outputs:
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
                    producers[output_artifact.id] = ProducerFile(path, collection_path)
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
                _verify_output_set(
                    workspace,
                    {output.path for output in sampler_result.outputs},
                )
                index_path = workspace.resolve("output/review/index.json")
                index_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                index_path.write_bytes(
                    canonical_json_bytes(
                        {
                            "format": "stove0-review-index/v1",
                            "variant_id": variant_id,
                            "sample_plan": sample_plan.model_dump(mode="json"),
                            "sampler_descriptor": descriptor.model_dump(mode="json"),
                            "sampler_request_sha256": sampler_request.request_sha256,
                            "sampler_result_sha256": sampler_result.result_sha256,
                            "samples": samples,
                        }
                    )
                )
                index_bytes, index_sha = _identity(index_path)
                index = OutputArtifact(
                    id="review-index",
                    role=REVIEW_INDEX_ROLE,
                    path="review/index.json",
                    bytes=index_bytes,
                    sha256=index_sha,
                    media_type="application/json",
                    derived_from=tuple(sorted(item.id for item, _claimed in resolved)),
                )
                artifacts.append(index)
                producers[index.id] = ProducerFile(index_path, index.path)
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
                    sampler_result.result_sha256,
                    declared,
                )
                if self.destination is not None:
                    manifest_path = workspace.resolve("control/delivery-manifest.json")
                    manifest_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    manifest_path.write_bytes(
                        canonical_json_bytes(
                            {
                                "format": "stove0-review-delivery-manifest/v1",
                                "delivery_id": request.declaration.job_id,
                                "artifacts": [item.model_dump(mode="json") for item in declared],
                            }
                        )
                    )
                    result = self.destination.commit(
                        delivery_id=request.declaration.job_id,
                        output_root=workspace.resolve("output"),
                        artifacts=declared,
                        manifest_path=manifest_path,
                    )
                    return execution.effect_success(
                        result,
                        operation=REVIEW_RCLONE_DELIVER_OPERATION,
                        execution_sha256=execution_sha256,
                        attempt=attempt,
                        runtime_evidence={
                            "image_digest": self.image_digest,
                            "sampler_descriptor_sha256": descriptor.descriptor_sha256,
                            "sampler_image_digest": descriptor.image_digest,
                            "sampler_result_sha256": sampler_result.result_sha256,
                        },
                    )
                return execution.publish_success(
                    producers,
                    artifacts=declared,
                    operation=REVIEW_MATERIALIZE_OPERATION,
                    execution_sha256=execution_sha256,
                    dispositions=dispositions,
                    attempt=attempt,
                    runtime_evidence={
                        "image_digest": self.image_digest,
                        "sampler_descriptor_sha256": descriptor.descriptor_sha256,
                        "sampler_image_digest": descriptor.image_digest,
                        "sampler_result_sha256": sampler_result.result_sha256,
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


def _identity(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(8 * 1024**2):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _verify_file(path: Path, expected_bytes: int, expected_sha256: str) -> None:
    size, sha256 = _identity(path)
    if (size, sha256) != (expected_bytes, expected_sha256):
        raise RuntimeError("sampler output differs from its declared identity")


def _verify_output_set(workspace: TransformWorkspace, expected: set[str]) -> None:
    output_root = workspace.resolve("output")
    actual = {
        path.relative_to(workspace.root).as_posix()
        for path in output_root.rglob("*")
        if path.is_file()
    }
    if actual != expected:
        raise RuntimeError("sampler workspace contains an undeclared output")


__all__ = [
    "COLLECTION_OPTIONS",
    "EFFECT_OPTIONS",
    "RcloneReviewDestination",
    "ReviewTargetService",
    "SamplerRegistration",
]

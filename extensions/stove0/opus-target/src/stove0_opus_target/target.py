"""One-operation Opus collection transform target."""

from __future__ import annotations

import importlib.metadata
import os
import threading
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from riverhog_api_client import ProducerFile
from riverhog_protocol import ArtifactDisposition, canonical_json_sha256
from stove0_media_archive_contracts import (
    AUDIO_ARCHIVE_OPERATION,
    AUDIO_ARCHIVE_ROLE,
    AudioArchiveIntent,
)
from stove0_protocol import JsonSchemaDocument
from stove0_target_support import (
    DEFAULT_TERMINAL_STATE_RETENTION_SECONDS,
    OutputArtifact,
    PersistentTargetService,
    TargetContract,
    TargetContractPayload,
    TargetExecutionCanceled,
    TargetExecutionInapplicable,
    TargetExecutionRuntime,
    TargetExecutionSession,
    TargetJobRequest,
    TargetJobStatus,
    TargetOperationSupport,
)

from stove0_opus_target.common import OpusContentError, file_identity, run_ffmpeg, tool_version

OPTIONS = JsonSchemaDocument.from_schema(
    "stove0.opus-target-options/v1",
    {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "ffmpeg_timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 86400}
        },
        "additionalProperties": False,
    },
)


def _version() -> str:
    try:
        return importlib.metadata.version("stove0-opus-target")
    except importlib.metadata.PackageNotFoundError:
        return "development"


class OpusTargetService(PersistentTargetService):
    def __init__(
        self,
        *,
        state_root: Path,
        workspace_root: Path,
        ffmpeg: str = "ffmpeg",
        source_revision: str = "unknown",
        image_digest: str,
        terminal_state_retention_seconds: int = DEFAULT_TERMINAL_STATE_RETENTION_SECONDS,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.workspace_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.workspace_root, 0o700)
        self.ffmpeg = ffmpeg
        self.image_digest = image_digest
        contract = TargetContract.seal(
            TargetContractPayload(
                implementation_id="stove0.opus-target/v1",
                implementation_version=_version(),
                source_revision=source_revision,
                image_digest=image_digest,
                operations=(
                    TargetOperationSupport(
                        operation_id=AUDIO_ARCHIVE_OPERATION.id,
                        operation_contract_sha256=AUDIO_ARCHIVE_OPERATION.contract_sha256,
                        options_schema=OPTIONS,
                    ),
                ),
            )
        )
        super().__init__(
            contract=contract,
            operations={AUDIO_ARCHIVE_OPERATION.id: AUDIO_ARCHIVE_OPERATION},
            state_root=state_root,
            execute=self._execute,
            terminal_state_retention_seconds=terminal_state_retention_seconds,
        )

    def _execute(
        self,
        request: TargetJobRequest,
        attempt: int,
        cancellation: threading.Event,
        session: TargetExecutionSession,
    ) -> TargetJobStatus:
        intent = AudioArchiveIntent.model_validate(request.declaration.plan.intent)
        timeout = request.declaration.plan.target_options.get("ffmpeg_timeout_seconds", 86400)
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            raise ValueError("ffmpeg_timeout_seconds must be an integer")

        def check() -> None:
            if cancellation.is_set():
                raise TargetExecutionCanceled("Opus target was canceled")

        with TargetExecutionRuntime.from_request(
            request,
            cancellation_check=check,
            producer_version=_version(),
            session=session,
        ) as execution:
            workspace = execution.open_workspace(self.workspace_root)
            try:
                resolved = execution.inputs()
                outputs: list[OutputArtifact] = []
                sources: dict[str, ProducerFile] = {}
                with execution.prepare_inputs(
                    tuple(item for item, _claimed in resolved)
                ) as retrieval:
                    for artifact, claimed in resolved:
                        check()
                        suffix = PurePosixPath(artifact.path).suffix[:32]
                        source = workspace.resolve(f"input/{artifact.id}{suffix}")
                        source.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        retrieval.download(claimed, source)
                        relative = f"audio/{artifact.id}.opus"
                        destination = workspace.resolve(f"output/{relative}")
                        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        temporary = destination.with_name(f".{destination.name}.part.opus")
                        try:
                            run_ffmpeg(
                                [
                                    self.ffmpeg,
                                    "-hide_banner",
                                    "-nostdin",
                                    "-y",
                                    "-i",
                                    str(source),
                                    "-vn",
                                    "-c:a",
                                    "libopus",
                                    "-b:a",
                                    f"{intent.bitrate_kbps}k",
                                    str(temporary),
                                ],
                                log_root=workspace.root,
                                timeout_seconds=timeout,
                                canceled=cancellation.is_set,
                            )
                        except OpusContentError as exc:
                            raise TargetExecutionInapplicable(
                                "opus-inapplicable", str(exc)
                            ) from exc
                        os.replace(temporary, destination)
                        size, sha256 = file_identity(destination)
                        output = OutputArtifact(
                            id=f"opus-{artifact.id}",
                            role=AUDIO_ARCHIVE_ROLE,
                            path=relative,
                            bytes=size,
                            sha256=sha256,
                            media_type="audio/ogg",
                            derived_from=(artifact.id,),
                        )
                        outputs.append(output)
                        sources[output.id] = ProducerFile(destination, relative)
                declared = tuple(sorted(outputs, key=lambda item: item.id))
                dispositions = tuple(
                    ArtifactDisposition(
                        input_collection_id=artifact.collection.collection_id,
                        input_manifest_sha256=artifact.collection.manifest_sha256,
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
                    declared,
                )
                return execution.publish_success(
                    sources,
                    artifacts=declared,
                    operation=AUDIO_ARCHIVE_OPERATION,
                    execution_sha256=execution_sha256,
                    dispositions=dispositions,
                    attempt=attempt,
                    runtime_evidence={
                        "ffmpeg": tool_version(self.ffmpeg),
                        "image_digest": self.image_digest,
                    },
                )
            finally:
                if not execution.published:
                    workspace.release()


def _execution_sha256(
    plan_sha256: str,
    image_digest: str,
    outputs: Sequence[OutputArtifact],
) -> str:
    """Identify exact Opus execution semantics independently of an attempt."""

    return canonical_json_sha256(
        {
            "format": "stove0-opus-target-execution/v1",
            "plan_sha256": plan_sha256,
            "image_digest": image_digest,
            "outputs": [item.model_dump(mode="json") for item in outputs],
        }
    )


__all__ = ["OPTIONS", "OpusTargetService"]

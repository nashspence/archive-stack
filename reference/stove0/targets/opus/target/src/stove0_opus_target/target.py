"""One-operation Opus collection transform target."""

from __future__ import annotations

import importlib.metadata
import os
import shutil
import threading
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from riverhog_api_client import ProducerFile
from riverhog_protocol import ArtifactDisposition, canonical_json_sha256
from stove0_media_archive_target_contracts import (
    AUDIO_ARCHIVE_OPERATION,
    AUDIO_ARCHIVE_ROLE,
    METADATA_XMP_ROLE,
    SOURCE_ARTIFACT_ROLE,
    AudioArchiveIntent,
)
from stove0_media_archive_target_support import (
    MediaArchiveProjection,
    ffmpeg_container_metadata_args,
    render_projection_xmp,
    resolve_media_archive_projection,
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
    TargetPreflightRequest,
    TargetPreflightResponse,
    TargetServiceError,
)

from stove0_opus_target.common import OpusContentError, file_identity, run_ffmpeg, tool_version

_PROJECTION_SCHEMA = MediaArchiveProjection.model_json_schema()
OPTIONS = JsonSchemaDocument.from_schema(
    "stove0.opus-target-options/v1",
    {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "ffmpeg_timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 86400},
            "media_projection": {
                key: value for key, value in _PROJECTION_SCHEMA.items() if key != "$defs"
            },
        },
        "$defs": _PROJECTION_SCHEMA.get("$defs", {}),
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

    def preflight(self, request: TargetPreflightRequest) -> TargetPreflightResponse:
        try:
            intent = AudioArchiveIntent.model_validate(request.intent)
            projection = resolve_media_archive_projection(
                inputs=request.inputs,
                observations=request.observations,
                policy=intent.metadata_projection,
                archive_directory="audio",
                archive_suffix=".opus",
            )
            supplied = request.target_options.get("media_projection")
            if (
                supplied is not None
                and MediaArchiveProjection.model_validate(supplied) != projection
            ):
                raise ValueError(
                    "configured media projection differs from exact observation evidence"
                )
        except (KeyError, ValueError) as exc:
            raise TargetServiceError(400, "invalid_target_request", str(exc)) from exc
        effective = request.model_copy(
            update={
                "target_options": {
                    **request.target_options,
                    "media_projection": projection.model_dump(mode="json"),
                }
            }
        )
        return super().preflight(effective)

    def _execute(
        self,
        request: TargetJobRequest,
        attempt: int,
        cancellation: threading.Event,
        session: TargetExecutionSession,
    ) -> TargetJobStatus:
        intent = AudioArchiveIntent.model_validate(request.declaration.plan.intent)
        options = request.declaration.plan.target_options
        timeout = options.get("ffmpeg_timeout_seconds", 86400)
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            raise ValueError("ffmpeg_timeout_seconds must be an integer")
        projection = MediaArchiveProjection.model_validate(options["media_projection"])
        try:
            projection.validate_plan_evidence(request.declaration.plan.observation_result_sha256s)
        except ValueError as error:
            raise RuntimeError("media projection differs from the target plan evidence") from error

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
                resolved_by_id = {
                    artifact.id: (artifact, claimed) for artifact, claimed in resolved
                }
                outputs: list[OutputArtifact] = []
                publication = execution.open_collection_publication()
                for item in projection.items:
                    check()
                    artifact, claimed = resolved_by_id[item.input_artifact_id]
                    suffix = PurePosixPath(artifact.path).suffix[:32]
                    source = workspace.resolve(f"input/{artifact.id}{suffix}")
                    source.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    with execution.prepare_inputs((artifact,)) as retrieval:
                        retrieval.download(claimed, source)
                    try:
                        relative = item.archive_path
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
                                    *ffmpeg_container_metadata_args(item),
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
                            id=_output_id("opus", item.derived_from),
                            role=AUDIO_ARCHIVE_ROLE,
                            path=relative,
                            bytes=size,
                            sha256=sha256,
                            media_type="audio/ogg",
                            derived_from=item.derived_from,
                        )
                        outputs.append(output)
                        publication.append(ProducerFile(destination, relative), output)
                        xmp = workspace.resolve(f"output/{item.xmp_path}")
                        xmp.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        xmp.write_bytes(
                            render_projection_xmp(item, tags=intent.metadata_projection.tags)
                        )
                        xmp_size, xmp_sha256 = file_identity(xmp)
                        xmp_output = OutputArtifact(
                            id=_output_id("metadata-xmp", item.derived_from),
                            role=METADATA_XMP_ROLE,
                            path=item.xmp_path,
                            bytes=xmp_size,
                            sha256=xmp_sha256,
                            media_type="application/rdf+xml",
                            derived_from=item.derived_from,
                        )
                        outputs.append(xmp_output)
                        publication.append(ProducerFile(xmp, item.xmp_path), xmp_output)
                    finally:
                        source.unlink(missing_ok=True)
                for retained in projection.retained_xmp_sidecars:
                    artifact, claimed = resolved_by_id[retained.input_artifact_id]
                    suffix = PurePosixPath(artifact.path).suffix[:32]
                    source = workspace.resolve(f"input/{artifact.id}{suffix}")
                    source.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    with execution.prepare_inputs((artifact,)) as retrieval:
                        retrieval.download(claimed, source)
                    try:
                        destination = workspace.resolve(f"output/{retained.output_path}")
                        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        shutil.copyfile(source, destination)
                        retained_size, retained_sha256 = file_identity(destination)
                        retained_output = OutputArtifact(
                            id=_output_id("source-xmp", (retained.input_artifact_id,)),
                            role=SOURCE_ARTIFACT_ROLE,
                            path=retained.output_path,
                            bytes=retained_size,
                            sha256=retained_sha256,
                            media_type="application/rdf+xml",
                            derived_from=(retained.input_artifact_id,),
                        )
                        outputs.append(retained_output)
                        publication.append(
                            ProducerFile(destination, retained.output_path),
                            retained_output,
                        )
                    finally:
                        source.unlink(missing_ok=True)
                declared = tuple(sorted(outputs, key=lambda item: item.id))
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
                    declared,
                )
                return publication.finish_success(
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
                if not execution.completed:
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


def _output_id(kind: str, derived_from: Sequence[str]) -> str:
    return (
        f"{kind}-{canonical_json_sha256({'kind': kind, 'derived_from': sorted(derived_from)})[:32]}"
    )


__all__ = ["OPTIONS", "OpusTargetService"]

"""One-operation NVENC AV1 + Opus collection transform target."""

from __future__ import annotations

import importlib.metadata
import os
import threading
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from riverhog_api_client import ProducerFile
from riverhog_protocol import ArtifactDisposition, canonical_json_sha256
from stove0_media_archive_contracts import (
    AV1_OPUS_ARCHIVE_OPERATION,
    AV1_OPUS_ARCHIVE_ROLE,
    SOURCE_ARTIFACT_ROLE,
    Av1OpusArchiveIntent,
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

from stove0_nvenc_av1_opus_target.common import (
    NvencContentError,
    file_identity,
    run_ffmpeg,
    tool_version,
)
from stove0_nvenc_av1_opus_target.media_source_artifacts import build_strict_source_artifacts

OPTIONS = JsonSchemaDocument.from_schema(
    "stove0.nvenc-av1-opus-target-options/v1",
    {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "ffmpeg_timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 86400},
            "preset": {"enum": ["p1", "p2", "p3", "p4", "p5", "p6", "p7"]},
        },
        "additionalProperties": False,
    },
)


def _version() -> str:
    try:
        return importlib.metadata.version("stove0-nvenc-av1-opus-target")
    except importlib.metadata.PackageNotFoundError:
        return "development"


class NvencAv1OpusTargetService(PersistentTargetService):
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
                implementation_id="stove0.nvenc-av1-opus-target/v1",
                implementation_version=_version(),
                source_revision=source_revision,
                image_digest=image_digest,
                operations=(
                    TargetOperationSupport(
                        operation_id=AV1_OPUS_ARCHIVE_OPERATION.id,
                        operation_contract_sha256=AV1_OPUS_ARCHIVE_OPERATION.contract_sha256,
                        options_schema=OPTIONS,
                    ),
                ),
            )
        )
        self.target_contract = contract
        super().__init__(
            contract=contract,
            operations={AV1_OPUS_ARCHIVE_OPERATION.id: AV1_OPUS_ARCHIVE_OPERATION},
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
        intent = Av1OpusArchiveIntent.model_validate(request.declaration.plan.intent)
        options = request.declaration.plan.target_options
        timeout = options.get("ffmpeg_timeout_seconds", 86400)
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            raise ValueError("ffmpeg_timeout_seconds must be an integer")
        preset = str(options.get("preset", "p7"))

        def check() -> None:
            if cancellation.is_set():
                raise TargetExecutionCanceled("NVENC AV1 + Opus target was canceled")

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
                        relative = f"video/{artifact.id}.mkv"
                        destination = workspace.resolve(f"output/{relative}")
                        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        command = self._command(source, destination, intent, preset)
                        effective = command
                        try:
                            run_ffmpeg(
                                command,
                                log_root=workspace.root,
                                timeout_seconds=timeout,
                                canceled=cancellation.is_set,
                            )
                        except NvencContentError as first:
                            if intent.salvage != "safe-remux":
                                raise TargetExecutionInapplicable(
                                    "nvenc-av1-opus-inapplicable", str(first)
                                ) from first
                            remuxed = workspace.resolve(f"salvage/{artifact.id}.mkv")
                            remuxed.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                            try:
                                run_ffmpeg(
                                    [
                                        self.ffmpeg,
                                        "-hide_banner",
                                        "-nostdin",
                                        "-y",
                                        "-err_detect",
                                        "ignore_err",
                                        "-i",
                                        str(source),
                                        "-map",
                                        "0",
                                        "-c",
                                        "copy",
                                        str(remuxed),
                                    ],
                                    log_root=workspace.root,
                                    timeout_seconds=timeout,
                                    canceled=cancellation.is_set,
                                )
                                effective = self._command(remuxed, destination, intent, preset)
                                run_ffmpeg(
                                    effective,
                                    log_root=workspace.root,
                                    timeout_seconds=timeout,
                                    canceled=cancellation.is_set,
                                )
                            except NvencContentError as exc:
                                raise TargetExecutionInapplicable(
                                    "nvenc-av1-opus-salvage-inapplicable", str(exc)
                                ) from exc
                        video = self._output(
                            destination,
                            artifact_id=f"video-{artifact.id}",
                            role=AV1_OPUS_ARCHIVE_ROLE,
                            path=relative,
                            media_type="video/x-matroska",
                            derived_from=(artifact.id,),
                        )
                        outputs.append(video)
                        sources[video.id] = ProducerFile(destination, relative)
                        bundle_relative = f"source-artifacts/{artifact.id}.tar.zst"
                        bundle = workspace.resolve(f"output/{bundle_relative}")
                        build_strict_source_artifacts(
                            source=source,
                            archive=destination,
                            bundle=bundle,
                            encode_command=effective,
                            intent=intent,
                            target_options=options,
                            target_contract_sha256=self.target_contract.contract_sha256,
                            plan_sha256=request.declaration.plan.plan_sha256,
                        )
                        source_artifact = self._output(
                            bundle,
                            artifact_id=f"source-artifacts-{artifact.id}",
                            role=SOURCE_ARTIFACT_ROLE,
                            path=bundle_relative,
                            media_type="application/zstd",
                            derived_from=(artifact.id,),
                        )
                        outputs.append(source_artifact)
                        sources[source_artifact.id] = ProducerFile(bundle, bundle_relative)
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
                    operation=AV1_OPUS_ARCHIVE_OPERATION,
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

    def _command(
        self,
        source: Path,
        destination: Path,
        intent: Av1OpusArchiveIntent,
        preset: str,
    ) -> list[str]:
        filters = (
            [] if intent.max_height is None else ["-vf", f"scale=-2:min(ih\\,{intent.max_height})"]
        )
        return [
            self.ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            *filters,
            "-c:v",
            "av1_nvenc",
            "-preset",
            preset,
            "-cq",
            str(intent.quality),
            "-c:a",
            "libopus",
            "-b:a",
            f"{intent.audio_bitrate_kbps}k",
            str(destination),
        ]

    @staticmethod
    def _output(
        source: Path,
        *,
        artifact_id: str,
        role: str,
        path: str,
        media_type: str,
        derived_from: tuple[str, ...],
    ) -> OutputArtifact:
        size, sha256 = file_identity(source)
        return OutputArtifact(
            id=artifact_id,
            role=role,
            path=path,
            bytes=size,
            sha256=sha256,
            media_type=media_type,
            derived_from=derived_from,
        )


def _execution_sha256(
    plan_sha256: str,
    image_digest: str,
    outputs: Sequence[OutputArtifact],
) -> str:
    """Identify exact AV1/Opus execution semantics independently of an attempt."""

    return canonical_json_sha256(
        {
            "format": "stove0-nvenc-av1-opus-target-execution/v1",
            "plan_sha256": plan_sha256,
            "image_digest": image_digest,
            "outputs": [item.model_dump(mode="json") for item in outputs],
        }
    )


__all__ = ["OPTIONS", "NvencAv1OpusTargetService"]

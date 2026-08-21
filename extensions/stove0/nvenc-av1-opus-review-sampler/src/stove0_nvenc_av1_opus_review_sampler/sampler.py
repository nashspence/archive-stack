"""Representative NVENC AV1 + Opus sampler with no collection authority."""

from __future__ import annotations

import importlib.metadata
import os
from pathlib import Path

from stove0_media_archive_contracts import AV1_OPUS_ARCHIVE_OPERATION, Av1OpusArchiveIntent
from stove0_review_contracts import REVIEW_VIDEO_ROLE
from stove0_review_sampler_protocol import (
    SamplerDescriptor,
    SamplerDescriptorPayload,
    SamplerFailure,
    SamplerOutput,
    SamplerRequest,
    SamplerResult,
    SamplerResultPayload,
)
from stove0_review_sampler_support import SamplerWorkspace

from stove0_nvenc_av1_opus_review_sampler.common import (
    NvencContentError,
    file_identity,
    run_ffmpeg,
    tool_version,
)


def _version() -> str:
    try:
        return importlib.metadata.version("stove0-nvenc-av1-opus-review-sampler")
    except importlib.metadata.PackageNotFoundError:
        return "development"


class NvencAv1OpusReviewSampler:
    def __init__(
        self,
        *,
        workspace_root: Path,
        ffmpeg: str = "ffmpeg",
        source_revision: str = "unknown",
        image_digest: str,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.ffmpeg = ffmpeg
        self._descriptor = SamplerDescriptor.seal(
            SamplerDescriptorPayload(
                implementation_id="stove0.nvenc-av1-opus-review-sampler/v1",
                implementation_version=_version(),
                source_revision=source_revision,
                image_digest=image_digest,
                primary_operation_id=AV1_OPUS_ARCHIVE_OPERATION.id,
                primary_operation_contract_sha256=AV1_OPUS_ARCHIVE_OPERATION.contract_sha256,
                portable_intent_schema=AV1_OPUS_ARCHIVE_OPERATION.intent_schema,
                output_role=REVIEW_VIDEO_ROLE,
            )
        )

    def descriptor(self) -> SamplerDescriptor:
        return self._descriptor

    def sample(self, request: SamplerRequest) -> SamplerResult:
        workspace = SamplerWorkspace(self.workspace_root, request)
        try:
            intent = Av1OpusArchiveIntent.model_validate(request.portable_intent)
            inputs = {item.id: workspace.verify_input(item) for item in request.inputs}
            outputs: list[SamplerOutput] = []
            total = 0
            for window in request.windows:
                if workspace.canceled():
                    return self._result(request, state="canceled")
                destination = workspace.output(window.output_path)
                temporary = destination.with_name(f".{destination.name}.part.mkv")
                filters = (
                    []
                    if intent.max_height is None
                    else ["-vf", f"scale=-2:min(ih\\,{intent.max_height})"]
                )
                try:
                    run_ffmpeg(
                        [
                            self.ffmpeg,
                            "-hide_banner",
                            "-nostdin",
                            "-y",
                            "-ss",
                            f"{window.start_ms / 1000:.3f}",
                            "-t",
                            f"{window.duration_ms / 1000:.3f}",
                            "-i",
                            str(inputs[window.input_id]),
                            *filters,
                            "-c:v",
                            "av1_nvenc",
                            "-preset",
                            "p7",
                            "-cq",
                            str(intent.quality),
                            "-c:a",
                            "libopus",
                            "-b:a",
                            f"{intent.audio_bitrate_kbps}k",
                            str(temporary),
                        ],
                        log_root=workspace.job_root / "control",
                        timeout_seconds=request.timeout_seconds,
                        canceled=workspace.canceled,
                    )
                except InterruptedError:
                    temporary.unlink(missing_ok=True)
                    return self._result(request, state="canceled")
                except NvencContentError as exc:
                    temporary.unlink(missing_ok=True)
                    return self._result(
                        request,
                        state="failed",
                        failure=SamplerFailure(
                            code="nvenc-av1-opus-inapplicable",
                            message=str(exc),
                            retryable=False,
                        ),
                    )
                os.replace(temporary, destination)
                size, sha256 = file_identity(destination)
                total += size
                if total > request.maximum_output_bytes:
                    destination.unlink(missing_ok=True)
                    return self._result(
                        request,
                        state="failed",
                        failure=SamplerFailure(
                            code="output-limit-exceeded",
                            message="NVENC AV1 + Opus samples exceed the sealed output limit.",
                            retryable=False,
                        ),
                    )
                outputs.append(
                    SamplerOutput(
                        id=window.id,
                        path=window.output_path,
                        bytes=size,
                        sha256=sha256,
                        media_type="video/x-matroska",
                        derived_from=(window.input_id,),
                    )
                )
            return self._result(request, state="succeeded", outputs=tuple(outputs))
        except Exception as exc:
            return self._result(
                request,
                state="failed",
                failure=SamplerFailure(
                    code="sampler-infrastructure",
                    message=f"{type(exc).__name__}: {exc}"[:1000],
                    retryable=True,
                ),
            )

    def _result(
        self,
        request: SamplerRequest,
        *,
        state: str,
        outputs: tuple[SamplerOutput, ...] = (),
        failure: SamplerFailure | None = None,
    ) -> SamplerResult:
        return SamplerResult.seal(
            SamplerResultPayload(
                request_sha256=request.request_sha256,
                sampler_descriptor_sha256=self._descriptor.descriptor_sha256,
                state=state,  # type: ignore[arg-type]
                outputs=outputs,
                failure=failure,
                execution_evidence={
                    "ffmpeg": tool_version(self.ffmpeg),
                    "image_digest": self._descriptor.image_digest,
                },
            )
        )


__all__ = ["NvencAv1OpusReviewSampler"]

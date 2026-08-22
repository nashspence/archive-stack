"""Representative Opus sampler running without Riverhog or Stove0 authority."""

from __future__ import annotations

import importlib.metadata
import os
from pathlib import Path

from stove0_media_archive_contracts import AUDIO_ARCHIVE_OPERATION, AudioArchiveIntent
from stove0_review_contracts import REVIEW_AUDIO_ROLE
from stove0_review_sampler_protocol import (
    SamplerDescriptor,
    SamplerDescriptorPayload,
    SamplerFailure,
    SamplerInapplicable,
    SamplerOutput,
    SamplerRequest,
    SamplerResult,
    SamplerResultPayload,
)
from stove0_review_sampler_support import SamplerWorkspace

from stove0_opus_review_sampler.common import (
    OpusContentError,
    file_identity,
    run_ffmpeg,
    tool_version,
)


def _version() -> str:
    try:
        return importlib.metadata.version("stove0-opus-review-sampler")
    except importlib.metadata.PackageNotFoundError:
        return "development"


class OpusReviewSampler:
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
                implementation_id="stove0.opus-review-sampler/v1",
                implementation_version=_version(),
                source_revision=source_revision,
                image_digest=image_digest,
                primary_operation_id=AUDIO_ARCHIVE_OPERATION.id,
                primary_operation_contract_sha256=AUDIO_ARCHIVE_OPERATION.contract_sha256,
                portable_intent_schema=AUDIO_ARCHIVE_OPERATION.intent_schema,
                output_role=REVIEW_AUDIO_ROLE,
            )
        )

    def descriptor(self) -> SamplerDescriptor:
        return self._descriptor

    def sample(self, request: SamplerRequest) -> SamplerResult:
        workspace = SamplerWorkspace(self.workspace_root, request)
        try:
            intent = AudioArchiveIntent.model_validate(request.portable_intent)
            inputs = {item.id: workspace.verify_input(item) for item in request.inputs}
            outputs: list[SamplerOutput] = []
            total = 0
            for window in request.windows:
                if workspace.canceled():
                    return self._result(request, state="canceled")
                destination = workspace.output(window.output_path)
                temporary = destination.with_name(f".{destination.name}.part.opus")
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
                            "-vn",
                            "-c:a",
                            "libopus",
                            "-b:a",
                            f"{intent.bitrate_kbps}k",
                            str(temporary),
                        ],
                        log_root=workspace.job_root / "control",
                        timeout_seconds=request.timeout_seconds,
                        canceled=workspace.canceled,
                    )
                except InterruptedError:
                    temporary.unlink(missing_ok=True)
                    return self._result(request, state="canceled")
                except OpusContentError as exc:
                    temporary.unlink(missing_ok=True)
                    return self._result(
                        request,
                        state="inapplicable",
                        inapplicable=SamplerInapplicable(
                            code="opus-inapplicable", message=str(exc)
                        ),
                    )
                os.replace(temporary, destination)
                size, sha256 = file_identity(destination)
                total += size
                if total > request.maximum_output_bytes:
                    destination.unlink(missing_ok=True)
                    return self._result(
                        request,
                        state="inapplicable",
                        inapplicable=SamplerInapplicable(
                            code="output-limit-exceeded",
                            message="Opus samples exceed the sealed output limit.",
                        ),
                    )
                outputs.append(
                    SamplerOutput(
                        id=window.id,
                        path=window.output_path,
                        bytes=size,
                        sha256=sha256,
                        media_type="audio/ogg",
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
        inapplicable: SamplerInapplicable | None = None,
    ) -> SamplerResult:
        return SamplerResult.seal(
            SamplerResultPayload(
                request_sha256=request.request_sha256,
                sampler_descriptor_sha256=self._descriptor.descriptor_sha256,
                state=state,  # type: ignore[arg-type]
                outputs=outputs,
                failure=failure,
                inapplicable=inapplicable,
                execution_evidence={
                    "ffmpeg": tool_version(self.ffmpeg),
                    "image_digest": self._descriptor.image_digest,
                },
            )
        )


__all__ = ["OpusReviewSampler"]

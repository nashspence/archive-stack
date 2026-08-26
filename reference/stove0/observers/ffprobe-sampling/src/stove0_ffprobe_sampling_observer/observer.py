"""FFprobe implementation of the maintained media-sampling contract."""

from __future__ import annotations

import importlib.metadata
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from stove0_media_sampling_observer_contracts import (
    MEDIA_SAMPLING_OBSERVER_CONTRACT,
    MediaSamplingArtifactFacts,
    MediaSamplingFacts,
    SampleableRange,
    validate_media_sampling_facts,
)
from stove0_observer_protocol import (
    ObservationRequest,
    ObservationResult,
    ObserverContractSupport,
    ObserverDescriptor,
    ObserverDescriptorPayload,
)
from stove0_observer_support import ObservationResultBuilder, ObservationRuntime


def _version() -> str:
    try:
        return importlib.metadata.version("stove0-ffprobe-sampling-observer")
    except importlib.metadata.PackageNotFoundError:
        return "development"


class FfprobeSamplingObserver:
    """Report bounded duration/sample ranges from exact immutable artifacts."""

    def __init__(
        self,
        *,
        ffprobe: str = "ffprobe",
        workspace_root: Path | None = None,
        source_revision: str = "unknown",
        image_digest: str,
    ) -> None:
        self.ffprobe = ffprobe
        self.workspace_root = (
            workspace_root or Path("/run/stove0-ffprobe-sampling-observer")
        ).resolve()
        self._descriptor = ObserverDescriptor.seal(
            ObserverDescriptorPayload(
                implementation_id="stove0.ffprobe-sampling-observer/v1",
                implementation_version=_version(),
                source_revision=source_revision,
                image_digest=image_digest,
                contracts=(
                    ObserverContractSupport.from_contract(MEDIA_SAMPLING_OBSERVER_CONTRACT),
                ),
            )
        )

    def descriptor(self) -> ObserverDescriptor:
        return self._descriptor

    def observe(
        self,
        request: ObservationRequest,
        runtime: ObservationRuntime,
    ) -> ObservationResult:
        builder = ObservationResultBuilder(self._descriptor, request)
        self.workspace_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.workspace_root, 0o700)
        workspace = runtime.open_workspace(self.workspace_root)
        try:
            facts: list[MediaSamplingArtifactFacts] = []
            for subject in request.subjects:
                runtime.heartbeat()
                source = runtime.materialize(
                    subject,
                    workspace=workspace,
                    relative_path=f"input/{subject.id}",
                )
                duration = self._probe(source, timeout_seconds=request.timeout_seconds)
                if duration is None:
                    return builder.inapplicable(
                        code="unsupported-media",
                        message="FFprobe found no usable media duration for this artifact.",
                        execution_evidence=self.execution_evidence(),
                    )
                facts.append(
                    MediaSamplingArtifactFacts(
                        artifact_id=subject.id,
                        duration_ms=duration,
                        sampleable_ranges=(SampleableRange(start_ms=0, duration_ms=duration),),
                    )
                )
            document = MediaSamplingFacts(artifacts=tuple(facts)).model_dump(mode="json")
            validate_media_sampling_facts(document, request.subjects)
            return builder.observed(
                document,
                execution_evidence=self.execution_evidence(),
            )
        except subprocess.TimeoutExpired:
            return builder.failed(
                code="observer-timeout",
                message="FFprobe sampling exceeded its sealed deadline.",
                retryable=True,
                execution_evidence=self.execution_evidence(),
            )
        finally:
            workspace.release()

    def execution_evidence(self) -> dict[str, str]:
        return {"ffprobe": _tool_version(self.ffprobe), "implementation": _version()}

    def _probe(self, source: Path, *, timeout_seconds: int) -> int | None:
        result = subprocess.run(
            [
                self.ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=duration",
                "-of",
                "json",
                str(source),
            ],
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
        )
        if result.returncode:
            return None
        try:
            payload = json.loads(result.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        durations: list[float] = []
        if isinstance(payload, dict):
            format_value = payload.get("format")
            if isinstance(format_value, dict):
                _append_duration(durations, format_value.get("duration"))
            streams = payload.get("streams")
            if isinstance(streams, list):
                for stream in streams:
                    if isinstance(stream, dict):
                        _append_duration(durations, stream.get("duration"))
        return max(1, round(max(durations) * 1000)) if durations else None


def _append_duration(values: list[float], value: Any) -> None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return
    if parsed > 0:
        values.append(parsed)


def _tool_version(command: str) -> str:
    try:
        result = subprocess.run(
            [command, "-version"], check=False, capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    lines = (result.stdout or result.stderr).splitlines()
    return lines[0][:200] if lines else "unavailable"


__all__ = ["FfprobeSamplingObserver"]

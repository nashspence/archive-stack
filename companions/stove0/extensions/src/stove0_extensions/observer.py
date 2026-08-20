"""Maintained media sampling observer implemented outside stove0 core."""

from __future__ import annotations

import importlib.metadata
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from stove0_observer_protocol import (
    ObservationRequest,
    ObservationResult,
    ObserverContractSupport,
    ObserverDescriptor,
    ObserverDescriptorPayload,
)
from stove0_observer_support import ObservationResultBuilder, ObservationRuntime
from stove0_review_contracts import (
    MEDIA_SAMPLING_OBSERVER_CONTRACT,
    MediaSamplingArtifactFacts,
    MediaSamplingFacts,
    SampleableRange,
)


def _version() -> str:
    try:
        return importlib.metadata.version("stove0-maintained-extensions")
    except importlib.metadata.PackageNotFoundError:
        return "development"


def _tool_version(command: str) -> str:
    try:
        result = subprocess.run(
            [command, "-version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    first = (result.stdout or result.stderr).splitlines()
    return first[0][:200] if first else "unavailable"


class MediaSamplingObserver:
    """Report bounded duration/sample ranges from exact immutable artifacts."""

    def __init__(self, *, ffprobe: str = "ffprobe") -> None:
        self.ffprobe = ffprobe
        self._descriptor = ObserverDescriptor.seal(
            ObserverDescriptorPayload(
                implementation_id="riverhog.media-sampling/v1",
                implementation_version=_version(),
                source_revision=os.getenv("STOVE0_EXTENSION_SOURCE_REVISION", "unknown"),
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
        workspace = runtime.open_workspace(_workspace_root())
        try:
            facts: list[MediaSamplingArtifactFacts] = []
            for subject in request.subjects:
                runtime.heartbeat()
                source = runtime.materialize(
                    subject,
                    workspace=workspace,
                    relative_path=f"input/{subject.id}",
                )
                observed = self._probe(source, timeout_seconds=request.timeout_seconds)
                if observed is None:
                    return builder.inapplicable(
                        execution_evidence={"ffprobe": _tool_version(self.ffprobe)}
                    )
                facts.append(
                    MediaSamplingArtifactFacts(
                        artifact_id=subject.id,
                        duration_ms=observed,
                        sampleable_ranges=(SampleableRange(start_ms=0, duration_ms=observed),),
                    )
                )
            document = MediaSamplingFacts(artifacts=tuple(facts)).model_dump(mode="json")
            return builder.observed(
                document,
                execution_evidence={
                    "ffprobe": _tool_version(self.ffprobe),
                    "implementation": _version(),
                },
            )
        except subprocess.TimeoutExpired:
            return builder.failed(
                code="observer-timeout",
                message="Media sampling exceeded its sealed deadline.",
                retryable=True,
                execution_evidence={"ffprobe": _tool_version(self.ffprobe)},
            )
        finally:
            workspace.release()

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
        if result.returncode != 0:
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
        if not durations:
            return None
        return max(1, round(max(durations) * 1000))


def _append_duration(values: list[float], value: Any) -> None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return
    if parsed > 0:
        values.append(parsed)


def _workspace_root() -> Path:
    root = Path(os.getenv("STOVE0_EXTENSION_WORKSPACE", "/run/stove0-workspaces"))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root


__all__ = ["MediaSamplingObserver"]

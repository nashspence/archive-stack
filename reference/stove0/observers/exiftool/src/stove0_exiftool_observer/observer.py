"""ExifTool implementation of the maintained media-metadata contract."""

from __future__ import annotations

import importlib.metadata
import json
import math
import os
import re
import subprocess
from pathlib import Path
from typing import Any, cast

from pydantic import JsonValue
from stove0_media_metadata_observer_contracts import (
    MEDIA_METADATA_OBSERVER_CONTRACT,
    MediaArtifactFacts,
    MediaFactEvidence,
    MediaFactName,
    MediaMetadataFact,
    MediaMetadataFacts,
)
from stove0_observer_protocol import (
    ObservationRequest,
    ObservationResult,
    ObserverContractSupport,
    ObserverDescriptor,
    ObserverDescriptorPayload,
    canonical_json_bytes,
)
from stove0_observer_support import ObservationResultBuilder, ObservationRuntime

_FACT_NAMES: dict[str, MediaFactName] = {
    "artist": "creator",
    "author": "creator",
    "byline": "creator",
    "createdate": "capture-time",
    "creationdate": "capture-time",
    "creator": "creator",
    "datetimeoriginal": "capture-time",
    "doctype": "container-format",
    "filetype": "container-format",
    "gpslatitude": "gps-latitude",
    "gpslongitude": "gps-longitude",
    "majorbrand": "container-format",
    "make": "device-make",
    "mediacreatedate": "capture-time",
    "mimetype": "container-format",
    "model": "device-model",
    "ownername": "creator",
    "trackcreatedate": "capture-time",
}
_SELECTED_FIELDS = (
    "Artist",
    "Author",
    "By-line",
    "CreateDate",
    "CreationDate",
    "Creator",
    "DateTimeOriginal",
    "DocType",
    "FileType",
    "GPSCoordinates",
    "GPSLatitude",
    "GPSLongitude",
    "MajorBrand",
    "Make",
    "MediaCreateDate",
    "MIMEType",
    "Model",
    "OwnerName",
    "TrackCreateDate",
)


def _version() -> str:
    try:
        return importlib.metadata.version("stove0-exiftool-observer")
    except importlib.metadata.PackageNotFoundError:
        return "development"


class ExiftoolObserver:
    """Report selected metadata with exact per-value evidence references."""

    def __init__(
        self,
        *,
        exiftool: str = "exiftool",
        workspace_root: Path | None = None,
        source_revision: str = "unknown",
        image_digest: str,
    ) -> None:
        self.exiftool = exiftool
        self.workspace_root = (workspace_root or Path("/run/stove0-exiftool-observer")).resolve()
        self._descriptor = ObserverDescriptor.seal(
            ObserverDescriptorPayload(
                implementation_id="stove0.exiftool-observer/v1",
                implementation_version=_version(),
                source_revision=source_revision,
                image_digest=image_digest,
                contracts=(
                    ObserverContractSupport.from_contract(MEDIA_METADATA_OBSERVER_CONTRACT),
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
            materialized: list[tuple[str, Path]] = []
            for subject in request.subjects:
                runtime.heartbeat()
                source = runtime.materialize(
                    subject,
                    workspace=workspace,
                    relative_path=f"input/{subject.id}/{Path(subject.path).name}",
                )
                materialized.append((subject.id, source))
            documents = self._probe(
                tuple(path for _artifact_id, path in materialized),
                timeout_seconds=request.timeout_seconds,
            )
            artifacts: list[MediaArtifactFacts] = []
            for artifact_id, source in materialized:
                document = documents.get(str(source))
                if document is None:
                    artifacts.append(
                        MediaArtifactFacts(
                            artifact_id=artifact_id,
                            state="unsupported",
                        )
                    )
                    continue
                facts = _metadata_facts(artifact_id, document)
                artifacts.append(
                    MediaArtifactFacts(
                        artifact_id=artifact_id,
                        state="observed" if facts else "unsupported",
                        facts=facts,
                    )
                )
            result = MediaMetadataFacts(
                artifacts=tuple(sorted(artifacts, key=lambda item: item.artifact_id))
            )
            return builder.observed(
                result.model_dump(mode="json"),
                execution_evidence=self.execution_evidence(),
            )
        except subprocess.TimeoutExpired:
            return builder.failed(
                code="observer-timeout",
                message="ExifTool observation exceeded its sealed deadline.",
                retryable=True,
                execution_evidence=self.execution_evidence(),
            )
        except OSError:
            return builder.failed(
                code="observer-unavailable",
                message="ExifTool could not be executed.",
                retryable=True,
                execution_evidence=self.execution_evidence(),
            )
        finally:
            workspace.release()

    def execution_evidence(self) -> dict[str, str]:
        return {"exiftool": _tool_version(self.exiftool), "implementation": _version()}

    def _probe(
        self,
        sources: tuple[Path, ...],
        *,
        timeout_seconds: int,
    ) -> dict[str, dict[str, Any]]:
        result = subprocess.run(
            [
                self.exiftool,
                "-j",
                "-G1",
                "-a",
                "-s",
                "-n",
                "-api",
                "LargeFileSupport=1",
                *(f"-{field}" for field in _SELECTED_FIELDS),
                *(str(source) for source in sources),
            ],
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
        )
        try:
            payload = json.loads(result.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, list):
            return {}
        expected = {str(source) for source in sources}
        documents: dict[str, dict[str, Any]] = {}
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            document = cast(dict[str, Any], raw)
            source_file = _source_file(document)
            if source_file in expected and source_file not in documents:
                documents[source_file] = document
        return documents


def _metadata_facts(artifact_id: str, document: dict[str, Any]) -> tuple[MediaMetadataFact, ...]:
    facts: list[MediaMetadataFact] = []
    for field, raw_value in document.items():
        if not isinstance(field, str):
            continue
        if _field_name(field) == "gpscoordinates":
            coordinates = _coordinates(raw_value)
            if coordinates is not None:
                for coordinate_name, coordinate_value in zip(
                    ("gps-latitude", "gps-longitude"),
                    coordinates,
                    strict=True,
                ):
                    facts.append(
                        MediaMetadataFact(
                            name=cast(MediaFactName, coordinate_name),
                            value=coordinate_value,
                            evidence=MediaFactEvidence(artifact_id=artifact_id, field=field),
                        )
                    )
            continue
        fact_name = _FACT_NAMES.get(_field_name(field))
        fact_value = _json_value(raw_value)
        if fact_name is None or fact_value is None:
            continue
        facts.append(
            MediaMetadataFact(
                name=fact_name,
                value=fact_value,
                evidence=MediaFactEvidence(artifact_id=artifact_id, field=field),
            )
        )
    return tuple(
        sorted(
            facts,
            key=lambda fact: (
                fact.name,
                fact.evidence.artifact_id,
                fact.evidence.field,
                canonical_json_bytes(fact.value),
            ),
        )
    )


def _field_name(value: str) -> str:
    return value.rsplit(":", 1)[-1].casefold().replace("-", "").replace("_", "")


def _source_file(document: dict[str, Any]) -> str | None:
    for field, value in document.items():
        if isinstance(field, str) and _field_name(field) == "sourcefile" and isinstance(value, str):
            return value
    return None


def _coordinates(value: Any) -> tuple[float, float] | None:
    if isinstance(value, list) and len(value) >= 2:
        latitude, longitude = value[:2]
        if all(isinstance(item, int | float) and not isinstance(item, bool) for item in value[:2]):
            result = float(latitude), float(longitude)
            return result if _valid_coordinates(*result) else None
    if not isinstance(value, str):
        return None
    values = re.findall(r"[+-]?\d+(?:\.\d+)?", value)
    if len(values) < 2:
        return None
    result = float(values[0]), float(values[1])
    return result if _valid_coordinates(*result) else None


def _valid_coordinates(latitude: float, longitude: float) -> bool:
    return (
        math.isfinite(latitude)
        and math.isfinite(longitude)
        and (-90 <= latitude <= 90 and -180 <= longitude <= 180)
    )


def _json_value(value: Any) -> JsonValue | None:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, list):
        normalized = [_json_value(item) for item in value]
        return normalized if all(item is not None for item in normalized) else None
    return None


def _tool_version(command: str) -> str:
    try:
        result = subprocess.run(
            [command, "-ver"], check=False, capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    lines = (result.stdout or result.stderr).splitlines()
    return lines[0][:200] if lines else "unavailable"


__all__ = ["ExiftoolObserver"]

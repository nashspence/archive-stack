from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

import yaml

from arc_core.planner.manifest import MANIFEST_FILENAME, manifest_dump, manifest_file_entry
from arc_core.recovery_payloads import (
    CommandAgeBatchpassRecoveryPayloadCodec,
    RecoveryPayloadCodec,
    decrypt_recovery_payload,
)


class CoveragePartRef(Protocol):
    collection_id: str
    path: str
    part_index: int
    part_count: int
    object_path: str | None
    sidecar_path: str | None


class CollectionArtifactRef(Protocol):
    collection_id: str
    manifest_path: str
    proof_path: str


@dataclass(frozen=True, slots=True)
class FinalizedImageCoveragePart:
    collection_id: str
    path: str
    part_index: int
    part_count: int
    object_path: str
    sidecar_path: str


@dataclass(frozen=True, slots=True)
class FinalizedImageCollectionArtifact:
    collection_id: str
    manifest_path: str
    proof_path: str


def read_finalized_image_collection_artifacts(
    image_root: str | Path,
    recovery_payload_codec: RecoveryPayloadCodec | None = None,
) -> list[FinalizedImageCollectionArtifact]:
    manifest = _read_disc_manifest(image_root, recovery_payload_codec)
    rows: list[FinalizedImageCollectionArtifact] = []
    collections = cast(list[dict[str, object]], manifest.get("collections", []))
    for collection in collections:
        rows.append(
            FinalizedImageCollectionArtifact(
                collection_id=str(collection["id"]),
                manifest_path=str(collection["manifest"]),
                proof_path=str(collection["proof"]),
            )
        )
    return rows


def read_finalized_image_coverage_parts(
    image_root: str | Path,
    recovery_payload_codec: RecoveryPayloadCodec | None = None,
) -> list[FinalizedImageCoveragePart]:
    manifest = _read_disc_manifest(image_root, recovery_payload_codec)
    rows: list[FinalizedImageCoveragePart] = []
    collections = cast(list[dict[str, object]], manifest.get("collections", []))
    for collection in collections:
        collection_id = str(collection["id"])
        for file_entry in cast(list[dict[str, object]], collection.get("files", [])):
            path = str(file_entry["path"]).lstrip("/")
            parts_block = file_entry.get("parts")
            if parts_block is None:
                rows.append(
                    FinalizedImageCoveragePart(
                        collection_id=collection_id,
                        path=path,
                        part_index=0,
                        part_count=1,
                        object_path=str(file_entry["object"]),
                        sidecar_path=str(file_entry["sidecar"]),
                    )
                )
                continue
            parts = cast(dict[str, object], parts_block)
            part_count = int(cast(int | str, parts["count"]))
            for present in cast(list[dict[str, object]], parts.get("present", [])):
                rows.append(
                    FinalizedImageCoveragePart(
                        collection_id=collection_id,
                        path=path,
                        part_index=int(cast(int | str, present["index"])) - 1,
                        part_count=part_count,
                        object_path=str(present["object"]),
                        sidecar_path=str(present["sidecar"]),
                    )
                )
    return rows


def build_disc_manifest_from_catalog(
    *,
    image_id: str,
    collection_artifacts: Sequence[CollectionArtifactRef],
    coverage_parts: Sequence[CoveragePartRef],
    file_lookup: Mapping[tuple[str, str], tuple[str, int]],
) -> bytes:
    artifacts_by_collection = {row.collection_id: row for row in collection_artifacts}
    grouped_parts = group_disc_manifest_entries(coverage_parts)
    payload: list[dict[str, object]] = []
    collections = sorted({collection_id for collection_id, _ in grouped_parts})
    for collection_id in collections:
        artifact = artifacts_by_collection.get(collection_id)
        if artifact is None:
            raise ValueError(
                f"missing finalized-image collection artifacts for {image_id}:{collection_id}"
            )
        files_payload: list[dict[str, object]] = []
        paths = sorted(
            path
            for current_collection_id, path in grouped_parts
            if current_collection_id == collection_id
        )
        for path in paths:
            file_meta = file_lookup.get((collection_id, path))
            if file_meta is None:
                raise ValueError(
                    f"missing collection file metadata for {image_id}:{collection_id}/{path}"
                )
            sha256, plaintext_bytes = file_meta
            parts = grouped_parts[(collection_id, path)]
            if len(parts) == 1 and parts[0][2] == 1:
                object_path, _, _, sidecar_path = parts[0]
                files_payload.append(
                    manifest_file_entry(
                        path,
                        sha256,
                        plaintext_bytes=plaintext_bytes,
                        object_path=object_path,
                        sidecar_path=sidecar_path,
                    )
                )
                continue
            part_count = parts[0][2]
            files_payload.append(
                manifest_file_entry(
                    path,
                    sha256,
                    plaintext_bytes=plaintext_bytes,
                    parts={
                        "count": part_count,
                        "present": [
                            {
                                "index": part_index + 1,
                                "object": object_path,
                                "sidecar": sidecar_path,
                            }
                            for object_path, part_index, _, sidecar_path in parts
                        ],
                    },
                )
            )
        payload.append(
            {
                "id": collection_id,
                "files": files_payload,
                "manifest": artifact.manifest_path,
                "proof": artifact.proof_path,
            }
        )
    return manifest_dump(image_id, payload)


def group_disc_manifest_entries(
    coverage_parts: Sequence[CoveragePartRef],
) -> dict[tuple[str, str], list[tuple[str, int, int, str]]]:
    grouped: dict[tuple[str, str], list[tuple[str, int, int, str]]] = defaultdict(list)
    for part in coverage_parts:
        if part.object_path is None or part.sidecar_path is None:
            raise ValueError(
                "finalized-image coverage part is missing persisted artifact paths for "
                f"{part.collection_id}/{part.path} part {part.part_index + 1}"
            )
        grouped[(part.collection_id, part.path)].append(
            (
                part.object_path,
                part.part_index,
                part.part_count,
                part.sidecar_path,
            )
        )
    for key in grouped:
        grouped[key].sort(key=lambda item: item[1])
    return dict(grouped)


def _read_disc_manifest(
    image_root: str | Path,
    recovery_payload_codec: RecoveryPayloadCodec | None,
) -> dict[str, object]:
    manifest_path = Path(image_root) / MANIFEST_FILENAME
    return cast(
        dict[str, object],
        yaml.safe_load(
            decrypt_recovery_payload(
                manifest_path.read_bytes(),
                recovery_payload_codec or _default_recovery_payload_codec(),
            )
        ),
    )


def _default_recovery_payload_codec() -> RecoveryPayloadCodec:
    from arc_core.runtime_config import load_runtime_config  # noqa: PLC0415

    config = load_runtime_config()
    return CommandAgeBatchpassRecoveryPayloadCodec(
        command=config.recovery_payload_command,
        passphrase=config.recovery_payload_passphrase,
        work_factor=config.recovery_payload_work_factor,
        max_work_factor=config.recovery_payload_max_work_factor,
    )

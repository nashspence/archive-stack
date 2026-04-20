from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from arc_core.iso import estimate_iso_size_from_root
from arc_core.planner.manifest import (
    MANIFEST_FILENAME,
    README_FILENAME,
    manifest_dump,
    manifest_file_entry,
    recovery_readme_bytes,
)

EncryptSize = Callable[[int], int]
IsoEstimator = Callable[..., int]


@dataclass(frozen=True)
class PreviewEntry:
    kind: str
    relpath: str
    size: int


@dataclass(frozen=True)
class PreviewImage:
    image_id: str
    used_bytes: int
    root_used_bytes: int
    iso_overhead_bytes: int
    free_bytes: int
    entries: list[PreviewEntry]


@dataclass(frozen=True)
class IsoLayoutPreview:
    image: PreviewImage
    payload_bytes: int
    collections: list[str]



def assign_paths(pieces: list[dict[str, object]]) -> dict[tuple[str, object, int], tuple[str, str]]:
    files = sorted(
        {(str(piece["collection"]), piece["file_id"], str(piece["relpath"])) for piece in pieces},
        key=lambda item: (item[0], item[2], str(item[1])),
    )
    base_index = {(collection, file_id): idx for idx, (collection, file_id, _) in enumerate(files)}
    out: dict[tuple[str, object, int], tuple[str, str]] = {}
    for piece in pieces:
        base = base_index[(str(piece["collection"]), piece["file_id"])]
        width = max(3, len(str(int(piece["piece_count"]))))
        payload_relpath = f"files/{base}"
        if int(piece["piece_count"]) > 1:
            payload_relpath += f".{int(piece['piece_index']) + 1:0{width}d}"
        out[(str(piece["collection"]), piece["file_id"], int(piece["piece_index"]))] = (
            payload_relpath,
            f"{payload_relpath}.meta.yaml",
        )
    return out



def manifest_bytes(image_id: str, collections: dict[str, list[dict[str, object]]], path_map: dict[tuple[str, object, int], tuple[str, str]]) -> bytes:
    payload: list[dict[str, object]] = []
    for collection_id in sorted(collections):
        files_payload: list[dict[str, object]] = []
        for file_meta in sorted(collections[collection_id], key=lambda item: str(item["relpath"])):
            present = sorted(file_meta["pieces"], key=lambda item: int(item["piece_index"]))
            if int(file_meta["piece_count"]) > 1:
                archive: object = {
                    "count": int(file_meta["piece_count"]),
                    "chunks": [
                        path_map[(collection_id, file_meta["file_id"], int(piece["piece_index"]))][0]
                        for piece in present
                    ],
                }
            elif present:
                archive = path_map[(collection_id, file_meta["file_id"], 0)][0]
            else:
                archive = None
            if archive is None:
                files_payload.append(manifest_file_entry(str(file_meta["relpath"]), str(file_meta["sha256"])))
            else:
                files_payload.append(
                    manifest_file_entry(str(file_meta["relpath"]), str(file_meta["sha256"]), archive)
                )
        payload.append({"name": collection_id, "files": files_payload})
    return manifest_dump(image_id, payload)



def _write_placeholder_file(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.truncate(size)



def preview_image(
    *,
    image_id: str,
    target_bytes: int,
    collections: dict[str, list[dict[str, object]]],
    pieces: list[dict[str, object]],
    encrypt_size: EncryptSize,
    estimate_iso_size: IsoEstimator | None = None,
    artifact_entries: list[PreviewEntry] | None = None,
) -> IsoLayoutPreview:
    estimator = estimate_iso_size or estimate_iso_size_from_root

    path_map = assign_paths(pieces)
    manifest = manifest_bytes(image_id, collections, path_map)
    readme = recovery_readme_bytes(image_id)

    entries: list[PreviewEntry] = [
        PreviewEntry(kind="manifest", relpath=MANIFEST_FILENAME, size=encrypt_size(len(manifest))),
        PreviewEntry(kind="readme", relpath=README_FILENAME, size=len(readme)),
    ]
    if artifact_entries:
        entries.extend(artifact_entries)

    payload_bytes = 0
    for piece in pieces:
        payload_relpath, sidecar_relpath = path_map[(str(piece["collection"]), piece["file_id"], int(piece["piece_index"]))]
        payload_size = int(piece["stored_size_bytes"])
        payload_bytes += payload_size
        entries.append(PreviewEntry(kind="payload", relpath=payload_relpath, size=payload_size))
        entries.append(
            PreviewEntry(
                kind="sidecar",
                relpath=sidecar_relpath,
                size=int(piece["sidecar_size_bytes"]),
            )
        )

    root_used = sum(entry.size for entry in entries)
    fallback = root_used
    with tempfile.TemporaryDirectory(prefix=".arc-preview-") as tmp_dir:
        root = Path(tmp_dir) / image_id
        for entry in entries:
            _write_placeholder_file(root / entry.relpath, entry.size)
        used = estimator(
            image_root=root,
            volume_id=image_id,
            fallback_bytes=fallback,
        )

    preview = PreviewImage(
        image_id=image_id,
        used_bytes=used,
        root_used_bytes=root_used,
        iso_overhead_bytes=max(0, used - root_used),
        free_bytes=target_bytes - used,
        entries=entries,
    )
    return IsoLayoutPreview(
        image=preview,
        payload_bytes=payload_bytes,
        collections=sorted(collections),
    )

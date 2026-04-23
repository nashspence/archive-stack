from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from arc_core.iso import estimate_iso_size_from_root
from arc_core.planner.manifest import (
    MANIFEST_FILENAME,
    README_FILENAME,
    assign_collection_artifact_paths,
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
    file_width = max(6, len(str(len(files) or 1)))
    base_index = {(collection, file_id): idx for idx, (collection, file_id, _) in enumerate(files)}
    out: dict[tuple[str, object, int], tuple[str, str]] = {}
    for piece in pieces:
        base = base_index[(str(piece["collection"]), piece["file_id"])]
        piece_width = max(3, len(str(int(piece["piece_count"]))))
        stem = f"files/{base + 1:0{file_width}d}"
        if int(piece["piece_count"]) > 1:
            stem += f".{int(piece['piece_index']) + 1:0{piece_width}d}"
        out[(str(piece["collection"]), piece["file_id"], int(piece["piece_index"]))] = (
            f"{stem}.age",
            f"{stem}.yml.age",
        )
    return out


def manifest_bytes(
    image_id: str,
    collections: dict[str, list[dict[str, object]]],
    path_map: dict[tuple[str, object, int], tuple[str, str]],
    *,
    volume_id: str | None = None,
    collection_artifact_paths: dict[str, tuple[str, str]] | None = None,
) -> bytes:
    payload: list[dict[str, object]] = []
    for collection_id in sorted(collections):
        files_payload: list[dict[str, object]] = []
        for file_meta in sorted(collections[collection_id], key=lambda item: str(item["relpath"])):
            present = sorted(file_meta["pieces"], key=lambda item: int(item["piece_index"]))
            if int(file_meta["piece_count"]) > 1:
                parts: object = {
                    "count": int(file_meta["piece_count"]),
                    "present": [
                        {
                            "index": int(piece["piece_index"]) + 1,
                            "object": path_map[
                                (collection_id, file_meta["file_id"], int(piece["piece_index"]))
                            ][0],
                            "sidecar": path_map[
                                (collection_id, file_meta["file_id"], int(piece["piece_index"]))
                            ][1],
                        }
                        for piece in present
                    ],
                }
            else:
                parts = None
            plaintext_bytes = file_meta.get("plaintext_bytes")
            if parts is None:
                object_path, sidecar_path = path_map[(collection_id, file_meta["file_id"], 0)]
                files_payload.append(
                    manifest_file_entry(
                        str(file_meta["relpath"]),
                        str(file_meta["sha256"]),
                        plaintext_bytes=int(plaintext_bytes)
                        if plaintext_bytes is not None
                        else None,
                        object_path=object_path,
                        sidecar_path=sidecar_path,
                    )
                )
            else:
                files_payload.append(
                    manifest_file_entry(
                        str(file_meta["relpath"]),
                        str(file_meta["sha256"]),
                        plaintext_bytes=int(plaintext_bytes)
                        if plaintext_bytes is not None
                        else None,
                        parts=parts,
                    )
                )
        collection_payload: dict[str, object] = {"id": collection_id, "files": files_payload}
        if collection_artifact_paths is not None:
            collection_manifest_path, proof_path = collection_artifact_paths[collection_id]
            collection_payload["manifest"] = collection_manifest_path
            collection_payload["proof"] = proof_path
        payload.append(collection_payload)
    return manifest_dump(volume_id or image_id, payload)


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
    volume_id: str | None = None,
) -> IsoLayoutPreview:
    estimator = estimate_iso_size or estimate_iso_size_from_root

    path_map = assign_paths(pieces)
    artifact_paths = assign_collection_artifact_paths(collections) if artifact_entries else None
    manifest = manifest_bytes(
        image_id,
        collections,
        path_map,
        volume_id=volume_id,
        collection_artifact_paths=artifact_paths,
    )
    readme = recovery_readme_bytes(image_id)

    entries: list[PreviewEntry] = [
        PreviewEntry(kind="manifest", relpath=MANIFEST_FILENAME, size=encrypt_size(len(manifest))),
        PreviewEntry(kind="readme", relpath=README_FILENAME, size=len(readme)),
    ]
    if artifact_entries:
        entries.extend(artifact_entries)

    payload_bytes = 0
    for piece in pieces:
        payload_relpath, sidecar_relpath = path_map[
            (str(piece["collection"]), piece["file_id"], int(piece["piece_index"]))
        ]
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

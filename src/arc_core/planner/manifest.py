from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, NotRequired, Required, TypedDict

import yaml

MANIFEST_FILENAME = "DISC.yml.age"
README_FILENAME = "README.md"
MANIFEST_SCHEMA = "disc-manifest/v1"
PLACEHOLDER_IMAGE_ID = "00000000T000000Z"
PLACEHOLDER_OBJECT = "files/999999.age"
PLACEHOLDER_SIDECAR = "files/999999.yml.age"
PLACEHOLDER_COLLECTION_MANIFEST = "collections/999999.yml.age"
PLACEHOLDER_COLLECTION_PROOF = "collections/999999.ots.age"
PLACEHOLDER_CHUNK_COUNT = 999999

_MISSING = object()


class ManifestPartRef(TypedDict):
    index: int
    object: str
    sidecar: str


class ManifestParts(TypedDict):
    count: int
    present: list[ManifestPartRef]


class PlannerFileMeta(TypedDict, total=False):
    relpath: Required[str]
    sha256: Required[str]
    plaintext_bytes: Required[int]
    mode: NotRequired[int | None]
    mtime: NotRequired[float | int | None]
    uid: NotRequired[int | None]
    gid: NotRequired[int | None]


def yaml_bytes(obj: Any) -> bytes:
    return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True).encode("utf-8")


def _manifest_relpath(path: str) -> str:
    normalized = path.strip()
    if not normalized.startswith("/"):
        normalized = f"/{normalized.lstrip('/')}"
    return normalized


def assign_collection_artifact_paths(collection_ids: Iterable[str]) -> dict[str, tuple[str, str]]:
    ordered = sorted(collection_ids)
    width = max(6, len(str(len(ordered) or 1)))
    return {
        collection_id: (
            f"collections/{index:0{width}d}.yml.age",
            f"collections/{index:0{width}d}.ots.age",
        )
        for index, collection_id in enumerate(ordered, start=1)
    }


def manifest_file_entry(
    path: str,
    sha256: str,
    *,
    plaintext_bytes: int | None = None,
    object_path: object = _MISSING,
    sidecar_path: object = _MISSING,
    parts: object = _MISSING,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "path": _manifest_relpath(path),
        "sha256": sha256,
    }
    if plaintext_bytes is not None:
        entry["bytes"] = plaintext_bytes
    if object_path is not _MISSING:
        entry["object"] = object_path
    if sidecar_path is not _MISSING:
        entry["sidecar"] = sidecar_path
    if parts is not _MISSING:
        entry["parts"] = parts
    return entry


def manifest_dump(image_id: str, collections_payload: Sequence[Mapping[str, object]]) -> bytes:
    return yaml_bytes(
        {
            "schema": MANIFEST_SCHEMA,
            "image": {
                "id": image_id,
            },
            "collections": collections_payload,
        }
    )


EMPTY_MANIFEST_SIZE = len(manifest_dump(PLACEHOLDER_IMAGE_ID, []))


def sidecar_dict(
    file_meta: PlannerFileMeta,
    *,
    collection_id: str,
    part_index: int = 0,
    part_count: int = 1,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "schema": "file-sidecar/v1",
        "collection": collection_id,
        "path": _manifest_relpath(file_meta["relpath"]),
        "sha256": file_meta["sha256"],
        "bytes": file_meta["plaintext_bytes"],
        "mode": file_meta.get("mode"),
        "mtime": file_meta.get("mtime"),
    }
    uid = file_meta.get("uid")
    if uid is not None:
        data["uid"] = uid
    gid = file_meta.get("gid")
    if gid is not None:
        data["gid"] = gid
    if part_count > 1:
        data["part"] = {"index": part_index + 1, "count": part_count}
    return data


def sidecar_bytes(
    file_meta: PlannerFileMeta,
    *,
    collection_id: str,
    part_index: int = 0,
    part_count: int = 1,
) -> bytes:
    return yaml_bytes(
        sidecar_dict(
            file_meta,
            collection_id=collection_id,
            part_index=part_index,
            part_count=part_count,
        )
    )


def manifest_collection_budget(collection_id: str, files: list[PlannerFileMeta]) -> int:
    payload = [
        {
            "id": collection_id,
            "manifest": PLACEHOLDER_COLLECTION_MANIFEST,
            "proof": PLACEHOLDER_COLLECTION_PROOF,
            "files": [
                manifest_file_entry(
                    file_meta["relpath"],
                    file_meta["sha256"],
                    plaintext_bytes=file_meta["plaintext_bytes"],
                    parts={
                        "count": PLACEHOLDER_CHUNK_COUNT,
                        "present": [
                            {
                                "index": PLACEHOLDER_CHUNK_COUNT,
                                "object": PLACEHOLDER_OBJECT,
                                "sidecar": PLACEHOLDER_SIDECAR,
                            }
                        ],
                    },
                )
                for file_meta in sorted(files, key=lambda item: item["relpath"])
            ],
        }
    ]
    return len(manifest_dump(PLACEHOLDER_IMAGE_ID, payload)) - EMPTY_MANIFEST_SIZE


def recovery_readme_bytes(container_name: str) -> bytes:
    lines = [
        f"Archive image: {container_name}",
        "",
        "This README.md is intentionally plaintext.",
        "Every other file on this disc is expected to be encrypted with age-plugin-batchpass.",
        "",
        "Preferred recovery flow:",
        "- use arc-disc with the API fetch manifest when available",
        "",
        "Manual recovery without arc-disc:",
        "- decrypt DISC.yml.age",
        "- find the collection and file entry you need",
        "- decrypt the paired files/*.yml.age sidecar for metadata",
        "- decrypt the matching files/*.age payload",
        (
            "- if a file is split, gather every listed part from every disc and concatenate "
            "plaintext in ascending part index order"
        ),
        (
            "- if a collection spans multiple discs, gather every disc whose DISC.yml.age "
            "lists that collection and reconstruct files by collection id plus path"
        ),
        (
            "- decrypt collections/*.yml.age and collections/*.ots.age to verify the "
            "whole-collection manifest and timestamp proof"
        ),
        "",
    ]
    return "\n".join(lines).encode("utf-8")

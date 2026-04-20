from __future__ import annotations

from typing import Any

import yaml

MANIFEST_FILENAME = "MANIFEST.yml"
README_FILENAME = "README.txt"
MANIFEST_SCHEMA = "manifest/v1"
PLACEHOLDER_CONTAINER = "00000000T000000Z"
PLACEHOLDER_ARCHIVE = "files/999999999999.999999"
PLACEHOLDER_CHUNK_COUNT = 999999

_MISSING = object()


def yaml_bytes(obj: Any) -> bytes:
    return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True).encode("utf-8")


def manifest_file_entry(path: str, sha256: str, archive: object = _MISSING) -> dict[str, object]:
    entry: dict[str, object] = {"path": path, "sha256": sha256}
    if archive is not _MISSING:
        entry["archive"] = archive
    return entry



def manifest_dump(container: str, collections_payload: list[dict[str, object]]) -> bytes:
    return yaml_bytes(
        {
            "schema": MANIFEST_SCHEMA,
            "container": container,
            "collections": collections_payload,
        }
    )


EMPTY_MANIFEST_SIZE = len(manifest_dump(PLACEHOLDER_CONTAINER, []))


def sidecar_dict(file_meta: dict[str, Any], part_index: int = 0, part_count: int = 1) -> dict[str, Any]:
    data: dict[str, Any] = {
        "schema": "sidecar/v1",
        "path": file_meta["relpath"],
        "sha256": file_meta["sha256"],
        "size": file_meta["plaintext_bytes"],
        "mode": file_meta.get("mode"),
        "mtime": file_meta.get("mtime"),
    }
    if file_meta.get("uid") is not None:
        data["uid"] = file_meta["uid"]
    if file_meta.get("gid") is not None:
        data["gid"] = file_meta["gid"]
    if part_count > 1:
        data["part"] = {"index": part_index + 1, "count": part_count}
    return data



def sidecar_bytes(file_meta: dict[str, Any], part_index: int = 0, part_count: int = 1) -> bytes:
    return yaml_bytes(sidecar_dict(file_meta, part_index=part_index, part_count=part_count))



def manifest_collection_budget(collection_id: str, files: list[dict[str, Any]]) -> int:
    payload = [
        {
            "name": collection_id,
            "files": [
                manifest_file_entry(
                    file_meta["relpath"],
                    file_meta["sha256"],
                    {
                        "count": PLACEHOLDER_CHUNK_COUNT,
                        "chunks": [],
                    },
                )
                for file_meta in sorted(files, key=lambda item: item["relpath"])
            ],
        }
    ]
    return len(manifest_dump(PLACEHOLDER_CONTAINER, payload)) - EMPTY_MANIFEST_SIZE



def recovery_readme_bytes(container_name: str) -> bytes:
    lines = [
        f"Archive image: {container_name}",
        "",
        "This README.txt is intentionally plaintext.",
        "Every other leaf file in the image is expected to be age-encrypted.",
        "",
        "Suggested recovery flow:",
        "- decrypt MANIFEST.yml",
        "- inspect collections[*].files[*].archive",
        "- decrypt files/<entry>",
        "- decrypt files/<entry>.meta.yaml for metadata",
        "- concatenate split chunks in chunk-index order",
        "",
    ]
    return "\n".join(lines).encode("utf-8")

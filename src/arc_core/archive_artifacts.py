from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from arc_core.fs_paths import normalize_collection_id, normalize_relpath
from arc_core.hashing import canonical_tree_hash, file_sha256
from arc_core.proofs import ProofStamper, StubProofStamper

COLLECTION_HASH_MANIFEST_NAME = "HASHES.yml"
COLLECTION_HASH_PROOF_NAME = f"{COLLECTION_HASH_MANIFEST_NAME}.ots"
COLLECTION_HASH_MANIFEST_SCHEMA = "collection-hash-manifest/v1"


@dataclass(frozen=True)
class CollectionArtifactPaths:
    manifest_path: Path
    proof_path: Path


def collection_artifact_relpaths(collection_id: str) -> tuple[str, str]:
    name = normalize_collection_id(collection_id)
    return (
        f"collections/{name}/{COLLECTION_HASH_MANIFEST_NAME}",
        f"collections/{name}/{COLLECTION_HASH_PROOF_NAME}",
    )


def scan_collection_root(root: Path) -> tuple[list[str], list[dict[str, object]]]:
    if not root.exists():
        raise ValueError(f"collection source directory is missing: {root}")
    if not root.is_dir():
        raise ValueError(f"collection source is not a directory: {root}")

    directories: list[str] = []
    files: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_dir():
            directories.append(normalize_relpath(rel))
            continue
        if not path.is_file():
            continue
        stat = path.stat()
        files.append(
            {
                "relative_path": normalize_relpath(rel),
                "size_bytes": stat.st_size,
                "sha256": file_sha256(path),
            }
        )
    return directories, files


def build_collection_hash_manifest(collection_id: str, source_root: Path) -> dict[str, object]:
    directories, files = scan_collection_root(source_root)
    tree_sha256, total_bytes, rows = canonical_tree_hash(source_root)
    return {
        "schema": COLLECTION_HASH_MANIFEST_SCHEMA,
        "collection": normalize_collection_id(collection_id),
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "tree": {
            "sha256": tree_sha256,
            "total_bytes": total_bytes,
        },
        "directories": directories,
        "files": rows,
    }


def generate_collection_hash_artifacts(
    *,
    collection_id: str,
    source_root: Path,
    artifact_root: Path,
    stamper: ProofStamper | None = None,
) -> CollectionArtifactPaths:
    artifact_root.mkdir(parents=True, exist_ok=True)
    manifest_path = artifact_root / COLLECTION_HASH_MANIFEST_NAME
    manifest = build_collection_hash_manifest(collection_id, source_root)
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    proof_path = (stamper or StubProofStamper()).stamp(manifest_path)
    return CollectionArtifactPaths(manifest_path=manifest_path, proof_path=proof_path)

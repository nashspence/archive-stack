from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml

from arc_core.archive_artifacts import (
    COLLECTION_HASH_MANIFEST_NAME,
    COLLECTION_HASH_MANIFEST_SCHEMA,
)
from arc_core.fs_paths import path_parents
from arc_core.planner.layout import assign_paths, manifest_bytes
from arc_core.planner.manifest import (
    MANIFEST_FILENAME,
    README_FILENAME,
    assign_collection_artifact_paths,
    recovery_readme_bytes,
    sidecar_bytes,
)


STAGING_PATH = "/staging/photos-2024"
PHOTOS_COLLECTION_ID = "photos-2024"
DOCS_COLLECTION_ID = "docs"

INVOICE_TARGET = "docs:/tax/2022/invoice-123.pdf"
RECEIPT_TARGET = "docs:/tax/2022/receipt-456.pdf"
TAX_DIRECTORY_TARGET = "docs:/tax/2022/"

IMAGE_ID = "img_2026-04-20_01"
SECOND_IMAGE_ID = "img_2026-04-20_02"
TARGET_BYTES = 10_000
MIN_FILL_BYTES = 7_500
DEFAULT_COPY_CREATED_AT = "2026-04-20T12:00:00Z"

FIXTURE_AGE_PREFIX = b"fixture-age-plugin-batchpass/v1\n"

PHOTOS_2024_FILES: dict[str, bytes] = {
    "albums/japan/day-01.txt": b"arrived in tokyo\n",
    "albums/japan/day-02.txt": b"visited asakusa\n",
    "raw/img_0001.cr3": b"raw-image-0001\n",
    "raw/img_0002.cr3": b"raw-image-0002-longer\n",
}

DOCS_FILES: dict[str, bytes] = {
    "tax/2022/invoice-123.pdf": b"invoice 123 contents\n",
    "tax/2022/receipt-456.pdf": b"receipt 456 contents\n",
    "letters/cover.txt": b"cover letter\n",
}

ALL_COLLECTION_FILES: dict[str, dict[str, bytes]] = {
    DOCS_COLLECTION_ID: DOCS_FILES,
    PHOTOS_COLLECTION_ID: PHOTOS_2024_FILES,
}


def total_bytes(files: Mapping[str, bytes]) -> int:
    return sum(len(content) for content in files.values())


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fixture_encrypt_bytes(data: bytes) -> bytes:
    return FIXTURE_AGE_PREFIX + base64.b64encode(data) + b"\n"


def fixture_decrypt_bytes(data: bytes) -> bytes:
    if not data.startswith(FIXTURE_AGE_PREFIX):
        raise ValueError("fixture ciphertext is missing the expected prefix")
    return base64.b64decode(data[len(FIXTURE_AGE_PREFIX) :].strip(), validate=True)


def _collection_manifest_bytes(collection_id: str, files: Mapping[str, bytes]) -> bytes:
    directories = sorted({parent for relpath in files for parent in path_parents(relpath)})
    rows: list[dict[str, object]] = []
    total = 0
    tree_digest = hashlib.sha256()

    for relpath in sorted(files):
        content = files[relpath]
        sha256 = _sha256_bytes(content)
        size = len(content)
        total += size
        rows.append(
            {
                "relative_path": relpath,
                "size_bytes": size,
                "sha256": sha256,
            }
        )
        tree_digest.update(f"{relpath}\t{size}\t{sha256}\n".encode("utf-8"))

    return yaml.safe_dump(
        {
            "schema": COLLECTION_HASH_MANIFEST_SCHEMA,
            "collection": collection_id,
            "generated_at": DEFAULT_COPY_CREATED_AT,
            "tree": {
                "sha256": tree_digest.hexdigest(),
                "total_bytes": total,
            },
            "directories": directories,
            "files": rows,
        },
        sort_keys=False,
        allow_unicode=True,
    ).encode("utf-8")


def _collection_proof_bytes(manifest_bytes: bytes) -> bytes:
    digest = _sha256_bytes(manifest_bytes)
    return (
        "\n".join(
            [
                "OpenTimestamps stub proof v1",
                f"file: {COLLECTION_HASH_MANIFEST_NAME}",
                f"sha256: {digest}",
                "",
            ]
        ).encode("utf-8")
    )


def _build_image_files(
    *,
    image_id: str,
    volume_id: str,
    represented_paths: Mapping[str, tuple[str, ...]],
) -> dict[str, bytes]:
    collections_payload: dict[str, list[dict[str, object]]] = {}
    pieces: list[dict[str, object]] = []

    for collection_id in sorted(represented_paths):
        files_payload: list[dict[str, object]] = []
        for file_id, relpath in enumerate(sorted(represented_paths[collection_id]), start=1):
            content = ALL_COLLECTION_FILES[collection_id][relpath]
            file_meta: dict[str, object] = {
                "collection": collection_id,
                "file_id": file_id,
                "relpath": relpath,
                "plaintext_bytes": len(content),
                "mode": 0o644,
                "mtime": 1713614400,
                "uid": None,
                "gid": None,
                "sha256": _sha256_bytes(content),
                "piece_count": 1,
                "pieces": [
                    {
                        "collection": collection_id,
                        "file_id": file_id,
                        "relpath": relpath,
                        "piece_index": 0,
                        "piece_count": 1,
                    }
                ],
            }
            files_payload.append(file_meta)
            pieces.append(
                {
                    "collection": collection_id,
                    "file_id": file_id,
                    "relpath": relpath,
                    "piece_index": 0,
                    "piece_count": 1,
                }
            )
        collections_payload[collection_id] = files_payload

    path_map = assign_paths(pieces)
    collection_artifact_paths = assign_collection_artifact_paths(collections_payload)
    disc_manifest = manifest_bytes(
        image_id,
        collections_payload,
        path_map,
        volume_id=volume_id,
        collection_artifact_paths=collection_artifact_paths,
    )

    image_files: dict[str, bytes] = {
        README_FILENAME: recovery_readme_bytes(image_id),
        MANIFEST_FILENAME: fixture_encrypt_bytes(disc_manifest),
    }

    for collection_id in sorted(collections_payload):
        manifest_path, proof_path = collection_artifact_paths[collection_id]
        collection_manifest = _collection_manifest_bytes(collection_id, ALL_COLLECTION_FILES[collection_id])
        image_files[manifest_path] = fixture_encrypt_bytes(collection_manifest)
        image_files[proof_path] = fixture_encrypt_bytes(_collection_proof_bytes(collection_manifest))

        for file_meta in collections_payload[collection_id]:
            payload_path, sidecar_path = path_map[(collection_id, file_meta["file_id"], 0)]
            relpath = str(file_meta["relpath"])
            image_files[payload_path] = fixture_encrypt_bytes(ALL_COLLECTION_FILES[collection_id][relpath])
            image_files[sidecar_path] = fixture_encrypt_bytes(
                sidecar_bytes(file_meta, collection_id=collection_id)
            )

    return image_files


IMAGE_ONE_FILES: dict[str, bytes] = _build_image_files(
    image_id=IMAGE_ID,
    volume_id="ARC-IMG-20260420-01",
    represented_paths={
        DOCS_COLLECTION_ID: (
            "tax/2022/invoice-123.pdf",
            "tax/2022/receipt-456.pdf",
        )
    },
)

IMAGE_TWO_FILES: dict[str, bytes] = _build_image_files(
    image_id=SECOND_IMAGE_ID,
    volume_id="ARC-IMG-20260420-02",
    represented_paths={
        PHOTOS_COLLECTION_ID: (
            "albums/japan/day-01.txt",
        )
    },
)


PHOTOS_2024_FILE_COUNT = len(PHOTOS_2024_FILES)
PHOTOS_2024_TOTAL_BYTES = total_bytes(PHOTOS_2024_FILES)
DOCS_TOTAL_BYTES = total_bytes(DOCS_FILES)


@dataclass(frozen=True, slots=True)
class ImageFixture:
    id: str
    volume_id: str
    filename: str
    files: Mapping[str, bytes]
    bytes: int
    iso_ready: bool
    covered_paths: tuple[tuple[str, str], ...]


IMAGE_FIXTURES: tuple[ImageFixture, ...] = (
    ImageFixture(
        id=IMAGE_ID,
        volume_id="ARC-IMG-20260420-01",
        filename=f"{IMAGE_ID}.iso",
        files=IMAGE_ONE_FILES,
        bytes=8_200,
        iso_ready=True,
        covered_paths=(
            (DOCS_COLLECTION_ID, "tax/2022/invoice-123.pdf"),
            (DOCS_COLLECTION_ID, "tax/2022/receipt-456.pdf"),
        ),
    ),
    ImageFixture(
        id=SECOND_IMAGE_ID,
        volume_id="ARC-IMG-20260420-02",
        filename=f"{SECOND_IMAGE_ID}.iso",
        files=IMAGE_TWO_FILES,
        bytes=6_100,
        iso_ready=False,
        covered_paths=((PHOTOS_COLLECTION_ID, "albums/japan/day-01.txt"),),
    ),
)


def build_file_copy(*, copy_id: str, location: str, collection_id: str, path: str) -> dict[str, object]:
    normalized = path.replace("/", "-")
    return {
        "id": copy_id,
        "location": location,
        "disc_path": f"/copies/{copy_id}/{collection_id}-{normalized}.age",
        "enc": {
            "alg": "fixture-age",
            "fixture_key": f"{copy_id}:{collection_id}:{path}",
        },
    }


def write_tree(root: Path, files: Mapping[str, bytes]) -> Path:
    for relative_path, content in files.items():
        file_path = root / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(content)
    return root

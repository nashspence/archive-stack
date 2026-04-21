from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


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

IMAGE_ONE_FILES: dict[str, bytes] = {
    "README.txt": b"disc one readme\n",
    "manifest.json": b'{"image": "img_2026-04-20_01"}\n',
    "payload/docs/tax/2022/invoice-123.pdf": DOCS_FILES["tax/2022/invoice-123.pdf"],
    "payload/docs/tax/2022/receipt-456.pdf": DOCS_FILES["tax/2022/receipt-456.pdf"],
}

IMAGE_TWO_FILES: dict[str, bytes] = {
    "README.txt": b"disc two readme\n",
    "manifest.json": b'{"image": "img_2026-04-20_02"}\n',
    "payload/photos/albums/japan/day-01.txt": PHOTOS_2024_FILES["albums/japan/day-01.txt"],
}


def total_bytes(files: Mapping[str, bytes]) -> int:
    return sum(len(content) for content in files.values())


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

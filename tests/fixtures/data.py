from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

PHOTOS_COLLECTION_ID = 1
DOCS_COLLECTION_ID = 2

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

PHOTOS_2024_FILE_COUNT = len(PHOTOS_2024_FILES)
PHOTOS_2024_TOTAL_BYTES = sum(len(content) for content in PHOTOS_2024_FILES.values())
DOCS_TOTAL_BYTES = sum(len(content) for content in DOCS_FILES.values())


def write_tree(root: Path, files: Mapping[str, bytes]) -> Path:
    for relative_path, content in files.items():
        file_path = root / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(content)
    return root

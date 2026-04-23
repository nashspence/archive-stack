from __future__ import annotations

import hashlib
from pathlib import Path


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_tree_hash(root: Path) -> tuple[str, int, list[dict[str, object]]]:
    digest = hashlib.sha256()
    total = 0
    rows: list[dict[str, object]] = []
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        rel = path.relative_to(root).as_posix()
        size = path.stat().st_size
        sha = file_sha256(path)
        total += size
        rows.append({"relative_path": rel, "size_bytes": size, "sha256": sha})
        digest.update(f"{rel}\t{size}\t{sha}\n".encode())
    return digest.hexdigest(), total, rows

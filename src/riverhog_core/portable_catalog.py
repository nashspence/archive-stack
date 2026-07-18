from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable


def portable_collection_manifest(
    collection_id: str,
    files: Iterable[tuple[str, int, str]],
) -> tuple[dict[str, object], str]:
    payload: dict[str, object] = {
        "format": "riverhog-collection/v1",
        "collection": collection_id,
        "files": [
            {"path": path, "bytes": byte_count, "sha256": sha256}
            for path, byte_count, sha256 in sorted(files)
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return payload, hashlib.sha256(canonical).hexdigest()

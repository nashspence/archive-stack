from __future__ import annotations

import re
from pathlib import PurePosixPath

from arc_core.domain.errors import InvalidTarget
from arc_core.domain.models import Target
from arc_core.domain.types import CollectionId

_TARGET_COLLECTION_RE = re.compile(r"^[^:/][^:]*$")
_TARGET_WITH_PATH_RE = re.compile(r"^(?P<collection>[^:/][^:]*):(?P<path>/.*)$")


def parse_target(raw: str) -> Target:
    match = _TARGET_WITH_PATH_RE.match(raw)
    if match:
        collection = CollectionId(match.group("collection"))
        raw_path = match.group("path")
        if raw_path in {"/", ""}:
            raise InvalidTarget("empty path")
        if "//" in raw_path:
            raise InvalidTarget("repeated slash")
        is_dir = raw_path.endswith("/")
        body = raw_path[:-1] if is_dir else raw_path
        path = PurePosixPath(body)
        if str(path) != body:
            raise InvalidTarget("non-canonical path")
        if any(part in {".", ".."} for part in path.parts):
            raise InvalidTarget("dot segments not allowed")
        return Target(collection_id=collection, path=path, is_dir=is_dir)

    if _TARGET_COLLECTION_RE.match(raw):
        return Target(collection_id=CollectionId(raw), path=None, is_dir=False)

    raise InvalidTarget("invalid target syntax")

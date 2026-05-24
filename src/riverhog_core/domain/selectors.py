from __future__ import annotations

from pathlib import PurePosixPath

from riverhog_core.domain.errors import InvalidTarget
from riverhog_core.domain.models import Target


def parse_target(raw: str) -> Target:
    if not raw:
        raise InvalidTarget("empty target")
    if raw.startswith("/"):
        raise InvalidTarget("path must be relative")
    if "//" in raw:
        raise InvalidTarget("repeated slash")

    is_dir = raw.endswith("/")
    body = raw[:-1] if is_dir else raw
    if not body:
        raise InvalidTarget("empty target")

    path = PurePosixPath(body)
    if str(path) != body:
        raise InvalidTarget("non-canonical path")
    if any(part in {".", ".."} for part in path.parts):
        raise InvalidTarget("dot segments not allowed")
    if not is_dir and len(path.parts) == 1:
        raise InvalidTarget("bare collection selectors must end with '/'")

    return Target(path=path, is_dir=is_dir)

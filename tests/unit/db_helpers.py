from __future__ import annotations

from pathlib import Path


def sqlite_url(path: Path) -> str:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path}"

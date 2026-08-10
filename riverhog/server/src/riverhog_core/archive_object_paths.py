from __future__ import annotations


def archive_store_object_path(prefix: str, *parts: str) -> str:
    return "/".join(normalized for value in (prefix, *parts) if (normalized := value.strip("/")))

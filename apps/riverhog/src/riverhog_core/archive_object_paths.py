from __future__ import annotations

_ARCHIVE_FILENAMES = {
    "manifest.yml.age",
    "manifest.yml.ots.age",
}


def archive_store_object_path(prefix: str, *parts: str) -> str:
    return "/".join(normalized for value in (prefix, *parts) if (normalized := value.strip("/")))


def archive_storage_prefix_from_object_path(object_path: str | None) -> str | None:
    if not object_path:
        return None
    normalized = object_path.strip("/")
    if "/" not in normalized:
        return None
    prefix, filename = normalized.rsplit("/", 1)
    if filename not in _ARCHIVE_FILENAMES:
        return None
    return prefix or None


def archive_id_from_storage_prefix(
    *,
    archive_prefix: str,
    storage_prefix: str | None,
) -> str | None:
    if not storage_prefix:
        return None
    normalized_storage_prefix = storage_prefix.strip("/")
    archive_root = f"{archive_store_object_path(archive_prefix, 'archives')}/"
    if not normalized_storage_prefix.startswith(archive_root):
        return None
    archive_id = normalized_storage_prefix.removeprefix(archive_root).split("/", 1)[0]
    return archive_id or None

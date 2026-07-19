from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from riverhog_protocol.errors import InvalidPath

from riverhog_core.fs_paths import PathNormalizationError, normalize_collection_id

_YEAR_RE = re.compile(r"^\d{4}$")


@dataclass(frozen=True)
class LogicalPath:
    path: PurePosixPath
    is_directory: bool

    @property
    def canonical(self) -> str:
        value = str(self.path)
        return f"{value}/" if self.is_directory else value


def parse_logical_path(raw: str) -> LogicalPath:
    if not raw:
        raise InvalidPath("path must not be empty")
    if raw.startswith("/"):
        raise InvalidPath("path must be relative")
    if "//" in raw:
        raise InvalidPath("path must not contain repeated slashes")

    is_directory = raw.endswith("/")
    body = raw[:-1] if is_directory else raw
    if not body:
        raise InvalidPath("path must not be empty")

    path = PurePosixPath(body)
    if str(path) != body or any(part in {".", ".."} for part in path.parts):
        raise InvalidPath("path must be canonical")
    parts = path.parts
    if len(parts) == 1:
        if not is_directory or not _YEAR_RE.fullmatch(parts[0]):
            raise InvalidPath("a year path must use YYYY/")
        return LogicalPath(path=path, is_directory=True)

    try:
        normalize_collection_id(f"{parts[0]}/{parts[1]}")
    except PathNormalizationError as exc:
        raise InvalidPath("path must begin with a canonical collection id") from exc
    if len(parts) == 2 and not is_directory:
        raise InvalidPath("a collection path must end with '/'")
    return LogicalPath(path=path, is_directory=is_directory)

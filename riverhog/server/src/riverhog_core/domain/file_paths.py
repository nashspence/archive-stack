from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from riverhog_protocol.errors import InvalidPath
from riverhog_protocol.paths import (
    PathNormalizationError,
    normalize_collection_id,
)


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
        try:
            normalize_collection_id(parts[0])
        except PathNormalizationError as exc:
            raise InvalidPath("a collection path must begin with a canonical id") from exc
        if not is_directory:
            raise InvalidPath("a collection path must end with '/'")
        return LogicalPath(path=path, is_directory=True)

    try:
        normalize_collection_id(parts[0])
    except PathNormalizationError as exc:
        raise InvalidPath("path must begin with a canonical collection id") from exc
    return LogicalPath(path=path, is_directory=is_directory)

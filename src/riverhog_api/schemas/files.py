from __future__ import annotations

from riverhog_api.schemas.common import RiverhogModel


class CollectionFileOut(RiverhogModel):
    path: str
    bytes: int
    hot: bool
    archived: bool


class CollectionFilesResponse(RiverhogModel):
    collection_id: str
    page: int
    per_page: int
    total: int
    pages: int
    files: list[CollectionFileOut]


class FileStateOut(RiverhogModel):
    target: str
    collection: str
    path: str
    bytes: int
    sha256: str
    hot: bool
    archived: bool


class FilesResponse(RiverhogModel):
    target: str
    page: int
    per_page: int
    total: int
    pages: int
    files: list[FileStateOut]

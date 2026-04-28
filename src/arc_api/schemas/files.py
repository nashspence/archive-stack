from __future__ import annotations

from arc_api.schemas.common import ArcModel


class CollectionFileOut(ArcModel):
    path: str
    bytes: int
    hot: bool
    archived: bool


class CollectionFilesResponse(ArcModel):
    collection_id: str
    page: int
    per_page: int
    total: int
    pages: int
    files: list[CollectionFileOut]


class FileStateOut(ArcModel):
    target: str
    collection: str
    path: str
    bytes: int
    sha256: str
    hot: bool
    archived: bool


class FilesResponse(ArcModel):
    target: str
    page: int
    per_page: int
    total: int
    pages: int
    files: list[FileStateOut]

from __future__ import annotations

from riverhog_api.schemas.common import RiverhogModel


class FileStateOut(RiverhogModel):
    logical_path: str
    collection_id: str
    collection_path: str
    bytes: int
    sha256: str
    hot: bool


class FilesResponse(RiverhogModel):
    path: str
    page: int
    per_page: int
    total: int
    pages: int
    files: list[FileStateOut]

from __future__ import annotations

from riverhog_api.schemas.common import RiverhogModel


class FileStateOut(RiverhogModel):
    target: str
    collection: str
    path: str
    bytes: int
    sha256: str
    hot: bool


class FilesResponse(RiverhogModel):
    target: str
    page: int
    per_page: int
    total: int
    pages: int
    files: list[FileStateOut]

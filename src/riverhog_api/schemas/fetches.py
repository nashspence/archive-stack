from __future__ import annotations

from pydantic import Field

from riverhog_api.schemas.common import RiverhogModel
from riverhog_api.schemas.pins import FetchHintCopyOut, HotStatusOut


class FetchSummaryOut(RiverhogModel):
    id: str
    target: str
    state: str
    files: int
    bytes: int
    entries_total: int
    entries_pending: int
    entries_partial: int
    entries_byte_complete: int
    entries_uploaded: int
    uploaded_bytes: int
    missing_bytes: int
    upload_state_expires_at: str | None
    copies: list[FetchHintCopyOut]


class FetchManifestCopyOut(RiverhogModel):
    copy_: str = Field(alias="copy")
    volume_id: str
    location: str
    disc_path: str
    recovery_bytes: int
    recovery_sha256: str


class FetchManifestPartOut(RiverhogModel):
    index: int
    bytes: int
    sha256: str
    recovery_bytes: int
    copies: list[FetchManifestCopyOut]


class FetchManifestEntryOut(RiverhogModel):
    id: str
    collection_id: str
    path: str
    bytes: int
    sha256: str
    recovery_bytes: int
    upload_state: str
    uploaded_bytes: int
    upload_state_expires_at: str | None
    copies: list[FetchManifestCopyOut]
    parts: list[FetchManifestPartOut]


class FetchManifestResponse(RiverhogModel):
    id: str
    target: str
    entries: list[FetchManifestEntryOut]


class FetchUploadSessionResponse(RiverhogModel):
    entry: str
    protocol: str
    upload_url: str
    offset: int
    length: int
    checksum_algorithm: str
    expires_at: str | None


class CompleteFetchResponse(RiverhogModel):
    id: str
    state: str
    hot: HotStatusOut

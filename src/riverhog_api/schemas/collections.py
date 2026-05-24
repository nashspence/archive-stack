from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict

from riverhog_api.schemas.archive import CollectionArchiveManifestOut, GlacierArchiveOut
from riverhog_api.schemas.common import RiverhogModel
from riverhog_api.schemas.images import CopyOut


class CollectionUploadFileIn(RiverhogModel):
    path: str
    bytes: int
    sha256: str


class CreateOrResumeCollectionUploadRequest(RiverhogModel):
    slug: str
    files: list[CollectionUploadFileIn]
    ingest_source: str | None = None
    upload_timestamp: str | None = None


class CollectionSummaryOut(RiverhogModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    files: int
    bytes: int
    hot_bytes: int
    archived_bytes: int
    pending_bytes: int
    glacier: GlacierArchiveOut | None = None
    archive_manifest: CollectionArchiveManifestOut | None = None
    archive_format: str | None = None
    compression: str | None = None
    disc_coverage: CollectionDiscCoverageOut | None = None
    protection_state: str
    protected_bytes: int
    image_coverage: list[CollectionCoverageImageOut]


class CollectionCoverageImageOut(RiverhogModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    filename: str
    physical_protection_state: (
        Literal["unprotected", "partially_protected", "protected"] | None
    ) = None
    physical_copies_required: int
    physical_copies_registered: int
    physical_copies_verified: int
    physical_copies_missing: int
    covered_paths: list[str]
    copies: list[CopyOut]


class CollectionDiscCoverageOut(RiverhogModel):
    state: Literal["none", "partial", "full"]
    covered_bytes: int = 0
    verified_physical_bytes: int = 0


class ListCollectionsResponse(RiverhogModel):
    page: int
    per_page: int
    total: int
    pages: int
    collections: list[CollectionSummaryOut]


CollectionSummaryOut.model_rebuild()


class CollectionUploadFileOut(RiverhogModel):
    path: str
    bytes: int
    sha256: str
    upload_state: str
    uploaded_bytes: int
    upload_state_expires_at: str | None


class CollectionUploadSessionOut(RiverhogModel):
    collection_id: str
    ingest_source: str | None
    state: Literal["uploading", "archiving", "finalized", "failed"]
    files_total: int
    files_pending: int
    files_partial: int
    files_uploaded: int
    bytes_total: int
    uploaded_bytes: int
    missing_bytes: int
    upload_state_expires_at: str | None
    latest_failure: str | None = None
    files: list[CollectionUploadFileOut]
    collection: CollectionSummaryOut | None


class CollectionFileUploadSessionOut(RiverhogModel):
    path: str
    protocol: str
    upload_url: str
    offset: int
    length: int
    checksum_algorithm: str
    expires_at: str | None

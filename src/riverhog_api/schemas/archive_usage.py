from __future__ import annotations

from typing import Literal

from riverhog_api.schemas.archive import ArchiveOut, CollectionManifestOut
from riverhog_api.schemas.common import RiverhogModel


class ArchiveUsageTotalsOut(RiverhogModel):
    collections: int
    uploaded_collections: int
    measured_storage_bytes: int


class ArchiveUsageImageOut(RiverhogModel):
    id: str
    filename: str
    collection_ids: list[str]


class ArchiveCollectionContributionOut(RiverhogModel):
    image_id: str
    filename: str
    represented_bytes: int


class ArchiveUsageCollectionOut(RiverhogModel):
    id: str
    bytes: int
    archive: ArchiveOut | None = None
    collection_manifest: CollectionManifestOut | None = None
    archive_format: str | None = None
    compression: str | None = None
    measured_storage_bytes: int
    images: list[ArchiveCollectionContributionOut]


class ArchiveUsageSnapshotOut(RiverhogModel):
    captured_at: str
    uploaded_collections: int
    measured_storage_bytes: int


class ArchiveUsageReportOut(RiverhogModel):
    scope: Literal["all", "collection", "filtered"]
    measured_at: str
    totals: ArchiveUsageTotalsOut
    images: list[ArchiveUsageImageOut]
    collections: list[ArchiveUsageCollectionOut]
    history: list[ArchiveUsageSnapshotOut]

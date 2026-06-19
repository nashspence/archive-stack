from __future__ import annotations

from typing import Literal

from riverhog_api.schemas.archive import CollectionManifestOut, GlacierArchiveOut
from riverhog_api.schemas.common import RiverhogModel


class GlacierUsageTotalsOut(RiverhogModel):
    collections: int
    uploaded_collections: int
    measured_storage_bytes: int


class GlacierUsageImageOut(RiverhogModel):
    id: str
    filename: str
    collection_ids: list[str]


class GlacierCollectionContributionOut(RiverhogModel):
    image_id: str
    filename: str
    represented_bytes: int


class GlacierUsageCollectionOut(RiverhogModel):
    id: str
    bytes: int
    glacier: GlacierArchiveOut | None = None
    collection_manifest: CollectionManifestOut | None = None
    archive_format: str | None = None
    compression: str | None = None
    measured_storage_bytes: int
    images: list[GlacierCollectionContributionOut]


class GlacierUsageSnapshotOut(RiverhogModel):
    captured_at: str
    uploaded_collections: int
    measured_storage_bytes: int


class GlacierUsageReportOut(RiverhogModel):
    scope: Literal["all", "collection", "filtered"]
    measured_at: str
    totals: GlacierUsageTotalsOut
    images: list[GlacierUsageImageOut]
    collections: list[GlacierUsageCollectionOut]
    history: list[GlacierUsageSnapshotOut]

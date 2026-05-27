from __future__ import annotations

import pytest
from sqlalchemy import BigInteger
from sqlalchemy.orm import DeclarativeBase

from riverhog_core.catalog_models import (
    CollectionArchiveRecord,
    CollectionFileRecord,
    CollectionUploadFileRecord,
    FetchEntryRecord,
    FileCopyRecord,
    FinalizedImageRecord,
    GlacierUsageSnapshotRecord,
    PlannedCandidateRecord,
)


@pytest.mark.parametrize(
    ("model", "column"),
    [
        (CollectionFileRecord, "bytes"),
        (FileCopyRecord, "part_bytes"),
        (FileCopyRecord, "recovery_bytes"),
        (CollectionArchiveRecord, "stored_bytes"),
        (CollectionArchiveRecord, "manifest_stored_bytes"),
        (CollectionArchiveRecord, "ots_stored_bytes"),
        (PlannedCandidateRecord, "bytes"),
        (PlannedCandidateRecord, "target_bytes"),
        (PlannedCandidateRecord, "min_fill_bytes"),
        (FinalizedImageRecord, "bytes"),
        (FinalizedImageRecord, "target_bytes"),
        (GlacierUsageSnapshotRecord, "measured_storage_bytes"),
        (GlacierUsageSnapshotRecord, "estimated_billable_bytes"),
        (GlacierUsageSnapshotRecord, "archived_metadata_bytes_per_object"),
        (GlacierUsageSnapshotRecord, "standard_metadata_bytes_per_object"),
        (FetchEntryRecord, "bytes"),
        (FetchEntryRecord, "recovery_bytes"),
        (FetchEntryRecord, "uploaded_bytes"),
        (CollectionUploadFileRecord, "bytes"),
        (CollectionUploadFileRecord, "uploaded_bytes"),
    ],
)
def test_catalog_byte_columns_use_bigint(
    model: type[DeclarativeBase],
    column: str,
) -> None:
    assert isinstance(model.__table__.c[column].type, BigInteger)

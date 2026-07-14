from __future__ import annotations

import pytest
from sqlalchemy import BigInteger
from sqlalchemy.orm import DeclarativeBase

from riverhog_core.catalog_models import (
    ArchiveUsageSnapshotRecord,
    CollectionArchiveRecord,
    CollectionFileRecord,
    CollectionUploadFileRecord,
    FetchEntryRecord,
    FileDiscRecord,
    FinalizedImageRecord,
    PlannedCandidateRecord,
)


@pytest.mark.parametrize(
    ("model", "column"),
    [
        (CollectionFileRecord, "bytes"),
        (FileDiscRecord, "part_bytes"),
        (FileDiscRecord, "recovery_bytes"),
        (CollectionArchiveRecord, "stored_bytes"),
        (CollectionArchiveRecord, "manifest_stored_bytes"),
        (CollectionArchiveRecord, "ots_stored_bytes"),
        (PlannedCandidateRecord, "bytes"),
        (PlannedCandidateRecord, "target_bytes"),
        (PlannedCandidateRecord, "min_fill_bytes"),
        (FinalizedImageRecord, "bytes"),
        (FinalizedImageRecord, "target_bytes"),
        (ArchiveUsageSnapshotRecord, "measured_storage_bytes"),
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

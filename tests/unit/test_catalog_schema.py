from __future__ import annotations

import pytest
from sqlalchemy import BigInteger
from sqlalchemy.orm import DeclarativeBase

from riverhog_core.catalog_models import (
    ArchiveUsageSnapshotRecord,
    CollectionArchiveCopyRecord,
    CollectionFileRecord,
    CollectionUploadFileRecord,
)


@pytest.mark.parametrize(
    ("model", "column"),
    [
        (CollectionFileRecord, "bytes"),
        (CollectionArchiveCopyRecord, "stored_bytes"),
        (CollectionArchiveCopyRecord, "manifest_stored_bytes"),
        (CollectionArchiveCopyRecord, "ots_stored_bytes"),
        (ArchiveUsageSnapshotRecord, "measured_storage_bytes"),
        (CollectionUploadFileRecord, "bytes"),
        (CollectionUploadFileRecord, "uploaded_bytes"),
    ],
)
def test_catalog_byte_columns_use_bigint(
    model: type[DeclarativeBase],
    column: str,
) -> None:
    assert isinstance(model.__table__.c[column].type, BigInteger)

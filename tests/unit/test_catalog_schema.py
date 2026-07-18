from __future__ import annotations

import pytest
from sqlalchemy import BigInteger
from sqlalchemy.orm import DeclarativeBase

from riverhog_core.catalog_models import (
    ArchiveUsageSnapshotRecord,
    CollectionArchiveObjectRecord,
    CollectionFileRecord,
    CollectionUploadFileRecord,
)


@pytest.mark.parametrize(
    ("model", "column"),
    [
        (CollectionFileRecord, "bytes"),
        (CollectionArchiveObjectRecord, "plaintext_bytes"),
        (CollectionArchiveObjectRecord, "stored_bytes"),
        (ArchiveUsageSnapshotRecord, "measured_storage_bytes"),
        (CollectionUploadFileRecord, "bytes"),
        (CollectionUploadFileRecord, "ingress_bytes"),
        (CollectionUploadFileRecord, "ingress_uploaded_bytes"),
    ],
)
def test_catalog_byte_columns_use_bigint(
    model: type[DeclarativeBase],
    column: str,
) -> None:
    assert isinstance(model.__table__.c[column].type, BigInteger)

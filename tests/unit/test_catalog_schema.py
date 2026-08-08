from __future__ import annotations

import pytest
from riverhog_core.catalog_models import (
    CollectionArchiveObjectRecord,
    CollectionArchiveObjectUploadRecord,
    CollectionFileRecord,
    CollectionUploadFileRecord,
)
from sqlalchemy import BigInteger
from sqlalchemy.orm import DeclarativeBase


@pytest.mark.parametrize(
    ("model", "column"),
    [
        (CollectionFileRecord, "bytes"),
        (CollectionArchiveObjectRecord, "plaintext_bytes"),
        (CollectionArchiveObjectRecord, "stored_bytes"),
        (CollectionUploadFileRecord, "bytes"),
        (CollectionUploadFileRecord, "raw_part_plaintext_bytes"),
        (CollectionArchiveObjectUploadRecord, "plaintext_bytes"),
        (CollectionArchiveObjectUploadRecord, "source_bytes"),
        (CollectionArchiveObjectUploadRecord, "unit_plaintext_bytes"),
        (CollectionArchiveObjectUploadRecord, "uploaded_bytes"),
    ],
)
def test_catalog_byte_columns_use_bigint(
    model: type[DeclarativeBase],
    column: str,
) -> None:
    assert isinstance(model.__table__.c[column].type, BigInteger)

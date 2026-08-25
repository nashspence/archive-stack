"""Provider-agnostic Riverhog logical storage identities."""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

ARCHIVE_STORE_NAME_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"


def validate_archive_store_name(value: str) -> str:
    if not value or value.strip().casefold() != value:
        raise ValueError("archive store name must use lowercase letters, digits, and single dashes")
    return value


type ArchiveStoreName = Annotated[
    str,
    Field(pattern=ARCHIVE_STORE_NAME_PATTERN),
    AfterValidator(validate_archive_store_name),
]


class ArchiveCopyStoreSelectionDocument(BaseModel):
    """Canonical logical stores participating in one Riverhog archive copy."""

    model_config = ConfigDict(extra="forbid", strict=True)

    destination_store: ArchiveStoreName
    source_store: ArchiveStoreName | None = None

    @model_validator(mode="after")
    def validate_distinct_stores(self) -> Self:
        if self.source_store == self.destination_store:
            raise ValueError("archive copy source and destination stores must differ")
        return self


__all__ = [
    "ARCHIVE_STORE_NAME_PATTERN",
    "ArchiveCopyStoreSelectionDocument",
    "ArchiveStoreName",
    "validate_archive_store_name",
]

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from munchy.profiles import EncodeProfile

WorkflowMode = Literal["collection_archive", "review"]
CollectionArchiveDestination = Literal["target", "riverhog"]


class InputFileSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def _validate_profile_group_path(cls, value: str) -> str:
        path = value.strip()
        if not path:
            raise ValueError("input path must not be blank")
        if "\\" in path:
            raise ValueError("input path must use POSIX '/' separators")
        parsed = PurePosixPath(path)
        if parsed.is_absolute():
            raise ValueError("input path must be relative")
        parts = parsed.parts
        if len(parts) < 2:
            raise ValueError("input path must be shaped as <profile-group>/<file>")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("input path must not contain empty, '.', or '..' segments")
        return parsed.as_posix()

    @property
    def profile_group(self) -> str:
        return PurePosixPath(self.path).parts[0]


class StorageHint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_bytes: int = Field(ge=0)
    expected_archive_bytes: int | None = Field(default=None, ge=0)
    expected_review_bytes: int | None = Field(default=None, ge=0)


class MunchyJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str = Field(min_length=1)
    upload_id: str = Field(min_length=1)
    workflow_mode: WorkflowMode
    collection_archive_destination: CollectionArchiveDestination = "riverhog"
    collection_slug: str = Field(min_length=1)
    collection_timestamp: str = Field(min_length=1)
    files: tuple[InputFileSpec, ...] = Field(min_length=1)
    profile_groups: dict[str, EncodeProfile]
    storage_hint: StorageHint

    @model_validator(mode="after")
    def _all_file_groups_have_profiles(self) -> Self:
        missing = sorted(profile_groups_for_files(self.files) - set(self.profile_groups))
        if missing:
            raise ValueError("missing encode profile group(s): " + ", ".join(missing))
        return self


def profile_groups_for_files(files: tuple[InputFileSpec, ...]) -> set[str]:
    return {item.profile_group for item in files}

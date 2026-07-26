from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal, Self

from munchy_workflows.profiles import EncodeProfile
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

WorkflowMode = Literal["collection_archive", "review"]
HandoffDestination = str


class InputFileSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def _validate_group_path(cls, value: str) -> str:
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
            raise ValueError("input path must be shaped as <group>/<file>")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("input path must not contain empty, '.', or '..' segments")
        return parsed.as_posix()

    @property
    def group(self) -> str:
        return PurePosixPath(self.path).parts[0]


class StorageHint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_bytes: int = Field(ge=0)
    expected_archive_bytes: int | None = Field(default=None, ge=0)
    expected_review_bytes: int | None = Field(default=None, ge=0)


class MunchyJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str = Field(min_length=1)
    input_upload_id: str = Field(min_length=1)
    workflow_mode: WorkflowMode
    handoff_destination: HandoffDestination
    collection_tags: tuple[str, ...] = ()
    run_id: str = Field(min_length=1)
    files: tuple[InputFileSpec, ...] = Field(min_length=1)
    groups: dict[str, EncodeProfile]
    storage_hint: StorageHint

    @model_validator(mode="after")
    def _all_file_groups_are_configured(self) -> Self:
        missing = sorted(groups_for_files(self.files) - set(self.groups))
        if missing:
            raise ValueError("undefined group(s): " + ", ".join(missing))
        return self


def groups_for_files(files: tuple[InputFileSpec, ...]) -> set[str]:
    return {item.group for item in files}

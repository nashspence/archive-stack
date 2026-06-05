from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MUNCHY_PROFILE_TARGET: Final[Literal["munchy-av1-nvenc"]] = "munchy-av1-nvenc"
SUPPORTED_ARCHIVE_CONTAINERS = ("mkv", "webm")
_ATOM_NAME_RE = r"[0-9a-z_]{1,16}"
_ATOM_OFFSET_RE = r"(?:0x[0-9a-f]+|[0-9]+)"


class ProfileError(ValueError):
    """Raised when a munchy encode profile is structurally invalid."""


def normalize_artifact_drop_selector(value: str) -> str:
    selector = value.strip().lower()
    if re.fullmatch(r"stream:[0-9]+", selector):
        return selector
    if re.fullmatch(rf"(?:atom|top-level-atom):{_ATOM_NAME_RE}(?::[0-9]+)?", selector):
        return selector
    if re.fullmatch(rf"atom-offset:{_ATOM_OFFSET_RE}", selector):
        return selector
    raise ProfileError(
        "artifact drop selector must be stream:N, atom:TYPE, atom:TYPE:N, "
        "top-level-atom:TYPE, top-level-atom:TYPE:N, or atom-offset:OFFSET"
    )


class ArtifactDrop(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    selector: str
    reason: str

    @field_validator("selector")
    @classmethod
    def _normalize_selector(cls, value: str) -> str:
        return normalize_artifact_drop_selector(value)

    @field_validator("reason")
    @classmethod
    def _strip_reason(cls, value: str) -> str:
        reason = value.strip()
        if not reason:
            raise ValueError("artifact drop reason must not be blank")
        return reason


class SourcePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_drops: tuple[ArtifactDrop, ...] = ()

    @model_validator(mode="after")
    def _reject_duplicate_drops(self) -> Self:
        selectors = [item.selector for item in self.artifact_drops]
        duplicates = sorted({selector for selector in selectors if selectors.count(selector) > 1})
        if duplicates:
            raise ValueError(f"duplicate source artifact drop selector: {duplicates[0]}")
        return self

    def artifact_drop_reasons(self) -> dict[str, str]:
        return {item.selector: item.reason for item in self.artifact_drops}


class VideoArchiveProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    codec: Literal["av1"] = "av1"
    quality: int = Field(default=49, ge=0, le=63)
    preset: str | None = None
    pix_fmt: str = "yuv420p10le"
    gop: int | None = Field(default=None, ge=1)
    filter: str | None = None


class AudioArchiveProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    codec: Literal["opus"] = "opus"
    bitrate: str = "28k"
    vbr: bool = True
    compression_level: int = Field(default=10, ge=0, le=10)
    application: Literal["audio", "voip", "lowdelay"] = "audio"
    frame_duration_ms: int = Field(default=40, ge=2, le=120)
    sample_rate: int = Field(default=24000, ge=8000)
    channels: int = Field(default=1, ge=1, le=8)
    cutoff_hz: int | None = Field(default=12000, ge=1)


class ArchiveProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    container: Literal["mkv", "webm"] = "mkv"
    video: VideoArchiveProfile = Field(default_factory=VideoArchiveProfile)
    audio: AudioArchiveProfile = Field(default_factory=AudioArchiveProfile)


class EncodeProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target: Literal["munchy-av1-nvenc"] = MUNCHY_PROFILE_TARGET
    archive: ArchiveProfile = Field(default_factory=ArchiveProfile)
    source: SourcePolicy = Field(default_factory=SourcePolicy)
    description: str | None = None

    def runner_payload(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True, mode="json")


def _profile_document(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = raw.get("encode_profile")
    if nested is None:
        return raw
    if not isinstance(nested, Mapping):
        raise ProfileError("encode_profile must be a table/object")
    return cast(Mapping[str, Any], nested)


def load_encode_profile(path: Path) -> EncodeProfile:
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    return EncodeProfile.model_validate(_profile_document(raw))


def artifact_drop_reason_map(profile: EncodeProfile | Mapping[str, Any] | None) -> dict[str, str]:
    if profile is None:
        return {}
    if isinstance(profile, EncodeProfile):
        return profile.source.artifact_drop_reasons()
    return EncodeProfile.model_validate(_profile_document(profile)).source.artifact_drop_reasons()

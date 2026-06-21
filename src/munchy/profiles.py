from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

MUNCHY_PROFILE_TARGET: Final[Literal["munchy-av1-nvenc"]] = "munchy-av1-nvenc"
ArchiveContainer = Literal["mkv", "webm"]
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


class SourceArtifactDropProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    selector: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=1000)

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


class SourcePreservationProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_drops: tuple[SourceArtifactDropProfile, ...] = ()

    @model_validator(mode="after")
    def _reject_duplicate_drops(self) -> Self:
        selectors = [item.selector for item in self.artifact_drops]
        duplicates = sorted({selector for selector in selectors if selectors.count(selector) > 1})
        if duplicates:
            raise ValueError(f"duplicate source artifact drop selector: {duplicates[0]}")
        return self

    def artifact_drop_reasons(self) -> dict[str, str]:
        return {item.selector: item.reason for item in self.artifact_drops}


class ArchiveAudioProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    codec: Literal["opus"] = "opus"
    bitrate: str | None = Field(default=None, min_length=2, max_length=16)
    sample_rate: int | None = Field(default=None, ge=8000, le=192000)
    channels: int | None = Field(default=None, ge=1, le=8)
    application: Literal["audio", "voip", "lowdelay"] | None = None
    frame_duration: float | None = None
    cutoff: int | None = Field(default=None, ge=0, le=24000)
    compression_level: int | None = Field(default=None, ge=0, le=10)
    vbr: Literal["on", "off", "constrained"] | bool | None = None

    @field_validator("bitrate")
    @classmethod
    def validate_bitrate(cls, value: str | None) -> str | None:
        if value is None:
            return None
        lowered = value.strip().lower()
        number = lowered[:-1] if lowered.endswith(("k", "m")) else lowered
        try:
            parsed = float(number)
        except ValueError as exc:
            raise ValueError("bitrate must look like 28k, 128k, or 1m") from exc
        if parsed <= 0:
            raise ValueError("bitrate must look like 28k, 128k, or 1m")
        return lowered

    @field_validator("frame_duration")
    @classmethod
    def validate_frame_duration(cls, value: float | None) -> float | None:
        if value is None:
            return None
        allowed = {2.5, 5.0, 10.0, 20.0, 40.0, 60.0}
        if float(value) not in allowed:
            raise ValueError("frame_duration must be one of 2.5, 5, 10, 20, 40, or 60")
        return float(value)


class ArchiveEncodeProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    codec: Literal["av1_nvenc"] = "av1_nvenc"
    container: ArchiveContainer = "mkv"
    quality: int | None = Field(default=None, ge=0, le=63)
    max_height: int | None = Field(default=None, ge=2, le=4320)
    fps_mode: Literal["passthrough", "halve_60_to_30"] = "passthrough"
    output_fps: float | None = Field(default=None, gt=0, le=240)
    scale_flags: Literal[
        "fast_bilinear",
        "bilinear",
        "bicubic",
        "lanczos",
        "spline",
    ] = "lanczos"
    pix_fmt: Literal["p010le", "yuv420p"] | None = None
    preset: str | None = Field(default=None, min_length=2, max_length=8)
    tune: Literal["hq", "ll", "ull", "lossless", "uhq"] | None = None
    audio: ArchiveAudioProfile = Field(default_factory=ArchiveAudioProfile)

    @field_validator("preset")
    @classmethod
    def validate_preset(cls, value: str | None) -> str | None:
        if value is None:
            return None
        lowered = value.strip().lower()
        if lowered not in {"p1", "p2", "p3", "p4", "p5", "p6", "p7"}:
            raise ValueError("preset must be p1 through p7")
        return lowered


class EncodeProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    target: Literal["munchy-av1-nvenc"] = MUNCHY_PROFILE_TARGET
    name: str | None = Field(default=None, min_length=1, max_length=120)
    source: SourcePreservationProfile | None = None
    archive: ArchiveEncodeProfile = Field(default_factory=ArchiveEncodeProfile)

    def runner_payload(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True, mode="json")

    def artifact_drop_reasons(self) -> dict[str, str]:
        if self.source is None:
            return {}
        return self.source.artifact_drop_reasons()


def _profile_name(path: Path, profile: EncodeProfile) -> str:
    return profile.name or path.stem


def profiles_from_document(
    raw: Mapping[str, Any],
    *,
    source_name: str = "profile",
) -> dict[str, EncodeProfile]:
    profiles = raw.get("profiles")
    if profiles is not None:
        if not isinstance(profiles, Mapping):
            raise ProfileError("profiles must be a table/object")
        if not profiles:
            raise ProfileError("profiles must contain at least one profile")
        return {
            str(name): EncodeProfile.model_validate(cast(Mapping[str, Any], profile))
            for name, profile in profiles.items()
        }
    try:
        profile = EncodeProfile.model_validate(raw)
    except ValidationError as exc:
        raise ProfileError(
            f"{source_name} must be a runner encode profile or contain [profiles.<name>]"
        ) from exc
    return {_profile_name(Path(source_name), profile): profile}


def load_encode_profile(path: Path) -> EncodeProfile:
    profiles = load_encode_profiles(path)
    if len(profiles) != 1:
        raise ProfileError(f"{path} contains multiple profiles; use load_encode_profiles")
    return next(iter(profiles.values()))


def load_encode_profiles(path: Path) -> dict[str, EncodeProfile]:
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    return profiles_from_document(raw, source_name=str(path))


def artifact_drop_reason_map(profile: EncodeProfile | Mapping[str, Any] | None) -> dict[str, str]:
    if profile is None:
        return {}
    if isinstance(profile, EncodeProfile):
        return profile.artifact_drop_reasons()
    return EncodeProfile.model_validate(profile).artifact_drop_reasons()

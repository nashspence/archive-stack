"""Target-owned media metadata projection intent."""

from __future__ import annotations

import math
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from stove0_media_metadata_observer_contracts import MediaFactName


class ProjectionPolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MediaGps(ProjectionPolicyModel):
    latitude: float
    longitude: float

    @model_validator(mode="after")
    def valid_position(self) -> Self:
        if not (
            math.isfinite(self.latitude)
            and math.isfinite(self.longitude)
            and -90 <= self.latitude <= 90
            and -180 <= self.longitude <= 180
        ):
            raise ValueError("media projection GPS coordinates are invalid")
        return self


class MediaFieldPreference(ProjectionPolicyModel):
    name: MediaFactName
    fields: tuple[str, ...] = Field(min_length=1)

    @field_validator("fields")
    @classmethod
    def unique_fields(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or item != item.strip() for item in value):
            raise ValueError("media projection evidence fields must be canonical")
        if len(value) != len(set(value)):
            raise ValueError("media projection evidence fields must be unique")
        return value


class MediaProjectionPolicy(ProjectionPolicyModel):
    """Portable recipe-owned choices; omitted values are never manufactured."""

    format: Literal["stove0-media-projection-policy/v1"] = "stove0-media-projection-policy/v1"
    device_make: str | None = None
    device_model: str | None = None
    gps: MediaGps | None = None
    creators: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    field_preferences: tuple[MediaFieldPreference, ...] = ()

    @field_validator("device_make", "device_model")
    @classmethod
    def canonical_optional_text(cls, value: str | None) -> str | None:
        if value is not None and (not value or value != value.strip()):
            raise ValueError("configured media metadata must be canonical")
        return value

    @field_validator("creators")
    @classmethod
    def canonical_creators(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or item != item.strip() for item in value):
            raise ValueError("configured media creators must be canonical")
        if len(value) != len(set(value)):
            raise ValueError("configured media creators must be unique")
        return value

    @field_validator("tags")
    @classmethod
    def canonical_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or item != item.strip() for item in value):
            raise ValueError("configured media tags must be canonical")
        if value != tuple(sorted(set(value))):
            raise ValueError("configured media tags must be unique and ordered")
        return value

    @field_validator("field_preferences")
    @classmethod
    def canonical_preferences(
        cls,
        value: tuple[MediaFieldPreference, ...],
    ) -> tuple[MediaFieldPreference, ...]:
        names = [item.name for item in value]
        if names != sorted(names) or len(names) != len(set(names)):
            raise ValueError("media field preferences must be unique and ordered by fact name")
        return value


__all__ = ["MediaFieldPreference", "MediaGps", "MediaProjectionPolicy"]

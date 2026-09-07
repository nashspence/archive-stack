"""Strict configuration for the maintained FTP collection producer."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from riverhog_provenance.common import require_urn_uuid

CloseMode = Literal["stable", "explicit-flush"]
ProvenanceMode = Literal["capture", "omit"]


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceConfig(ConfigModel):
    """One deployment-owned, content-opaque intake source."""

    id: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,118}[a-z0-9])?$")
    root: Path
    ingest_source: str = Field(min_length=1, max_length=512)
    archive_store: str | None = Field(default=None, min_length=1, max_length=160)
    close_mode: CloseMode = "stable"
    stable_seconds: int = Field(default=30, ge=1, le=7 * 24 * 60 * 60)
    max_files: int = Field(default=1000, ge=1)
    max_bytes: int = Field(default=100 * 1024**3, ge=1)
    provenance: ProvenanceMode = "capture"
    provenance_omission_reason: str | None = Field(default=None, max_length=1000)

    @field_validator("root")
    @classmethod
    def absolute_root(cls, value: Path) -> Path:
        expanded = value.expanduser()
        if not expanded.is_absolute():
            raise ValueError("FTP adapter source root must be absolute")
        return expanded.resolve()

    @model_validator(mode="after")
    def complete_policy(self) -> Self:
        if self.provenance == "omit":
            reason = (self.provenance_omission_reason or "").strip()
            if not reason or reason != self.provenance_omission_reason:
                raise ValueError("omitted provenance requires a visible canonical reason")
        elif self.provenance_omission_reason is not None:
            raise ValueError("capture mode cannot declare a provenance omission reason")
        return self


class FtpAdapterConfig(ConfigModel):
    host_id: str = Field(min_length=1, max_length=255)
    riverhog_base_url: str = Field(min_length=1, max_length=2048)
    riverhog_token: str = Field(min_length=1, max_length=4096, repr=False)
    allow_insecure_http: bool = False
    api_token: str = Field(min_length=1, max_length=4096, repr=False)
    provenance_observer: str | None = Field(default=None, min_length=1, max_length=255)
    sources: tuple[SourceConfig, ...] = Field(min_length=1)
    poll_seconds: float = Field(default=5.0, ge=0.1, le=3600)

    @field_validator("sources")
    @classmethod
    def unique_sources(cls, value: tuple[SourceConfig, ...]) -> tuple[SourceConfig, ...]:
        ids = [item.id for item in value]
        roots = [item.root for item in value]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("FTP adapter sources must be unique and ordered by ID")
        if len(roots) != len(set(roots)):
            raise ValueError("FTP adapter source roots must be unique")
        return value

    @model_validator(mode="after")
    def provenance_authority(self) -> Self:
        captures = any(source.provenance == "capture" for source in self.sources)
        if captures:
            require_urn_uuid(self.host_id, "host_id")
            if self.provenance_observer is None:
                raise ValueError("captured provenance requires an explicit observer provider")
        elif self.provenance_observer is not None:
            raise ValueError("provenance observer is unused when every source omits provenance")
        return self

    def source(self, source_id: str) -> SourceConfig:
        for source in self.sources:
            if source.id == source_id:
                return source
        raise KeyError(source_id)


def load_config(path: Path | None = None) -> FtpAdapterConfig:
    raw_path = str(path) if path is not None else os.environ.get("RIVERHOG_FTP_ADAPTER_CONFIG", "")
    if not raw_path.strip():
        raise ValueError("RIVERHOG_FTP_ADAPTER_CONFIG is required")
    resolved = Path(raw_path).expanduser()
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("FTP adapter configuration must be a JSON object")
    base_url = os.environ.get("RIVERHOG_BASE_URL", "").strip()
    if base_url:
        payload["riverhog_base_url"] = base_url
    for field, variable in {
        "riverhog_token": "RIVERHOG_TOKEN",
        "api_token": "RIVERHOG_FTP_ADAPTER_API_TOKEN",
    }.items():
        value = _environment_secret(variable)
        if value is not None:
            payload[field] = value
    if "allow_insecure_http" not in payload:
        payload["allow_insecure_http"] = os.environ.get(
            "RIVERHOG_ALLOW_INSECURE_HTTP", "false"
        ).casefold() in {"1", "true", "yes", "on"}
    return FtpAdapterConfig.model_validate(payload)


def _environment_secret(name: str) -> str | None:
    direct = os.environ.get(name, "").strip()
    file_name = os.environ.get(f"{name}_FILE", "").strip()
    if direct and file_name:
        raise ValueError(f"{name} and {name}_FILE are mutually exclusive")
    value = (
        Path(file_name).expanduser().read_text(encoding="utf-8").strip() if file_name else direct
    )
    return value or None


__all__ = ["FtpAdapterConfig", "SourceConfig", "load_config"]

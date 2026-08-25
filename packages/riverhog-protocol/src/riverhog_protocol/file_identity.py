"""Canonical immutable identity for one file payload."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from riverhog_protocol.paths import CanonicalRelPath

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ImmutableFileIdentityDocument(BaseModel):
    """The exact path, length, and plaintext digest shared by file projections."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: CanonicalRelPath
    bytes: int = Field(ge=0)
    sha256: Sha256


__all__ = ["ImmutableFileIdentityDocument"]

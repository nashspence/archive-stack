"""Portable per-file binding vocabulary used by segmented provenance archives."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class FileProvenanceBinding:
    path: str
    bytes: int
    sha256: str
    status: Literal["captured", "omitted"]
    journal_id: str | None = None
    current_state_id: str | None = None
    omission_reason: str | None = None


__all__ = ["FileProvenanceBinding"]

"""Recovery-facing adapters for the canonical segmented provenance archive."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from riverhog_provenance import (
    FileProvenanceBinding,
    ProvenanceRootDocument,
    ProvenanceTerminalDocument,
    ProvenanceValidationError,
    ProvenanceVolumeDocument,
    parse_binding_segment,
)


class ProvenanceRecoveryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SegmentedProvenanceRoot:
    archive_generation: str
    archive_tree_sha256: str
    ordered_volume_sha256: str
    identity: str


@dataclass(frozen=True, slots=True)
class SegmentedProvenanceVolume:
    sequence: int
    archive_generation: str
    archive_tree_sha256: str
    payload_kind: Literal["bindings", "journal"]
    payload_path: str
    payload_bytes: int
    payload_sha256: str
    first_file_order: int | None = None
    file_count: int | None = None
    journal_id: str | None = None
    journal_offset: int | None = None
    journal_bytes: int | None = None
    journal_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class SegmentedProvenanceTerminal:
    sequence: int
    archive_generation: str
    archive_tree_sha256: str


def parse_segmented_provenance_root(content: bytes) -> SegmentedProvenanceRoot:
    try:
        root = ProvenanceRootDocument.from_json_bytes(content)
    except ProvenanceValidationError as exc:
        raise ProvenanceRecoveryError(str(exc)) from exc
    return SegmentedProvenanceRoot(
        archive_generation=root.archive_generation,
        archive_tree_sha256=root.archive_tree_sha256,
        ordered_volume_sha256=root.ordered_volume_sha256,
        identity=root.identity,
    )


def parse_segmented_provenance_volume(content: bytes) -> SegmentedProvenanceVolume:
    try:
        volume = ProvenanceVolumeDocument.from_json_bytes(content)
    except ProvenanceValidationError as exc:
        raise ProvenanceRecoveryError(str(exc)) from exc
    return SegmentedProvenanceVolume(
        sequence=volume.sequence,
        archive_generation=volume.archive_generation,
        archive_tree_sha256=volume.archive_tree_sha256,
        payload_kind=volume.payload.kind,
        payload_path=volume.payload.path,
        payload_bytes=volume.payload.bytes,
        payload_sha256=volume.payload.sha256,
        first_file_order=volume.first_file_order,
        file_count=volume.file_count,
        journal_id=volume.journal_id,
        journal_offset=volume.journal_offset,
        journal_bytes=volume.journal_bytes,
        journal_sha256=volume.journal_sha256,
    )


def parse_segmented_provenance_terminal(content: bytes) -> SegmentedProvenanceTerminal:
    try:
        terminal = ProvenanceTerminalDocument.from_json_bytes(content)
    except ProvenanceValidationError as exc:
        raise ProvenanceRecoveryError(str(exc)) from exc
    return SegmentedProvenanceTerminal(
        sequence=terminal.sequence,
        archive_generation=terminal.archive_generation,
        archive_tree_sha256=terminal.archive_tree_sha256,
    )


def parse_segmented_binding_payload(
    content: bytes,
) -> tuple[int, tuple[FileProvenanceBinding, ...]]:
    try:
        first, rows = parse_binding_segment(content)
        bindings = tuple(_binding(row) for row in rows)
    except (ProvenanceValidationError, TypeError, ValueError) as exc:
        raise ProvenanceRecoveryError(str(exc)) from exc
    return first, bindings


def _binding(row: dict[str, object]) -> FileProvenanceBinding:
    status = row.get("status")
    byte_count = row.get("bytes")
    if status not in {"captured", "omitted"}:
        raise ProvenanceRecoveryError("provenance binding status is invalid")
    if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0:
        raise ProvenanceRecoveryError("provenance binding byte count is invalid")
    return FileProvenanceBinding(
        path=str(row.get("path") or ""),
        bytes=byte_count,
        sha256=str(row.get("sha256") or ""),
        status=status,
        journal_id=(str(row["journal_id"]) if row.get("journal_id") is not None else None),
        current_state_id=(
            str(row["current_state_id"]) if row.get("current_state_id") is not None else None
        ),
        omission_reason=(
            str(row["omission_reason"]) if row.get("omission_reason") is not None else None
        ),
    )


__all__ = [
    "ProvenanceRecoveryError",
    "SegmentedProvenanceRoot",
    "SegmentedProvenanceTerminal",
    "SegmentedProvenanceVolume",
    "parse_segmented_binding_payload",
    "parse_segmented_provenance_root",
    "parse_segmented_provenance_terminal",
    "parse_segmented_provenance_volume",
]

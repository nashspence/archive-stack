from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .archive import FileProvenanceBinding
from .interface import FileStateObserver
from .journal import (
    JournalSummary,
    ProvenanceValidationError,
    append_observation,
    create_observation_journal,
    validate_journal,
    validate_journal_set,
    verify_payload_binding,
)
from .segmented_archive import (
    PROVENANCE_TERMINAL_SCHEMA,
    ProvenanceRootDocument,
    ProvenanceTerminalDocument,
    ProvenanceVolumeDocument,
    format_provenance_sequence,
    parse_binding_segment,
    update_ordered_volume_commitment,
)

SIDECAR_SUFFIX = ".riverhog-provenance.json-seq"


@dataclass(frozen=True, slots=True)
class PreparedFileProvenance:
    binding: FileProvenanceBinding
    journals: dict[str, bytes]
    source: str


@dataclass(frozen=True, slots=True)
class _ValidatedSegmentedSet:
    bindings: tuple[FileProvenanceBinding, ...]
    journals: dict[str, JournalSummary]
    journal_bytes: dict[str, bytes]
    identity: str


def canonical_sidecar_path(payload: Path) -> Path:
    return payload.with_name(payload.name + SIDECAR_SUFFIX)


def prepare_file_provenance(
    payload: Path,
    *,
    relative_path: str,
    host_id: str,
    agent_name: str,
    agent_version: str,
    observer: FileStateObserver | None = None,
    provenance: Path | None = None,
    omit_reason: str | None = None,
) -> PreparedFileProvenance:
    if provenance is not None and omit_reason is not None:
        raise ProvenanceValidationError("provenance input and omission are mutually exclusive")
    byte_count, sha256 = _payload_identity(payload)
    if omit_reason is not None:
        reason = omit_reason.strip()
        if not reason or reason != omit_reason:
            raise ProvenanceValidationError(
                "provenance omission requires a visible canonical reason"
            )
        return PreparedFileProvenance(
            binding=FileProvenanceBinding(
                path=relative_path,
                bytes=byte_count,
                sha256=sha256,
                status="omitted",
                omission_reason=reason,
            ),
            journals={},
            source="omitted",
        )

    discovered = provenance or _discover_provenance(payload)
    if discovered is None:
        if observer is None:
            raise ProvenanceValidationError("capturing provenance requires a native observer")
        journal = create_observation_journal(
            payload,
            relative_path=relative_path,
            host_id=host_id,
            agent_name=agent_name,
            agent_version=agent_version,
            observer=observer,
        )
        summary = validate_journal(journal)
        return _captured(
            relative_path, byte_count, sha256, {summary.journal_id: journal}, "captured"
        )
    if discovered.is_dir() or discovered.name == "root.json":
        root_path = discovered / "root.json" if discovered.is_dir() else discovered
        validated = _load_segmented_set(root_path)
        matches = [
            item
            for item in validated.bindings
            if item.status == "captured"
            and item.bytes == byte_count
            and item.sha256 == sha256
            and (item.path == relative_path or Path(item.path).name == payload.name)
        ]
        if len(matches) != 1:
            raise ProvenanceValidationError(
                "provenance set does not identify exactly one captured binding for the payload"
            )
        binding = matches[0]
        journal_id = str(binding.journal_id)
        journals = _journal_closure(journal_id, validated)
        current = journals[journal_id]
    else:
        current = discovered.read_bytes()
        summary = validate_journal(current)
        if summary.external_states:
            raise ProvenanceValidationError(
                "a journal with ancestor references must be supplied as a provenance set"
            )
        journal_id = summary.journal_id
        journals = {journal_id: current}

    summary = validate_journal(current)
    if summary.current_bytes != byte_count or summary.current_sha256 != sha256:
        raise ProvenanceValidationError("supplied provenance does not bind to the payload bytes")
    if summary.current_path != relative_path:
        if observer is None:
            raise ProvenanceValidationError("continuing provenance requires a native observer")
        continued = append_observation(
            current,
            payload,
            relative_path=relative_path,
            host_id=host_id,
            agent_name=agent_name,
            agent_version=agent_version,
            observer=observer,
        )
        if not continued.startswith(current):
            raise RuntimeError("continued provenance did not preserve its exact prefix")
        journals[summary.journal_id] = continued
    return _captured(relative_path, byte_count, sha256, journals, "continued")


def _captured(
    relative_path: str,
    byte_count: int,
    sha256: str,
    journals: dict[str, bytes],
    source: str,
) -> PreparedFileProvenance:
    current = [
        summary
        for summary in (validate_journal(content) for content in journals.values())
        if summary.current_path == relative_path
        and summary.current_bytes == byte_count
        and summary.current_sha256 == sha256
    ]
    if len(current) != 1:
        raise ProvenanceValidationError(
            "provenance does not have one current journal for the payload"
        )
    summary = current[0]
    return PreparedFileProvenance(
        binding=FileProvenanceBinding(
            path=relative_path,
            bytes=byte_count,
            sha256=sha256,
            status="captured",
            journal_id=summary.journal_id,
            current_state_id=summary.current_state_id,
        ),
        journals=journals,
        source=source,
    )


def _discover_provenance(payload: Path) -> Path | None:
    adjacent = canonical_sidecar_path(payload)
    if adjacent.is_file():
        return adjacent
    for parent in (payload.parent, *payload.parents):
        root = parent / ".riverhog" / "provenance" / "root.json"
        if root.is_file():
            return root
    return None


def _load_segmented_set(root_path: Path) -> _ValidatedSegmentedSet:
    root_dir = root_path.parent
    root = ProvenanceRootDocument.from_json_bytes(root_path.read_bytes())
    journal_dir = root_dir / "journals"
    journals: dict[str, bytes] = {}
    if journal_dir.is_dir():
        for path in sorted(journal_dir.glob("*.json-seq")):
            journal_id = path.name.removesuffix(".json-seq")
            journals[journal_id] = path.read_bytes()
    bindings: list[FileProvenanceBinding] = []
    ordered = hashlib.sha256()
    next_file_order = 0
    sequence = 0
    while True:
        sequence_token = format_provenance_sequence(sequence)
        metadata = (root_dir / "metadata" / f"volume-{sequence_token}.json").read_bytes()
        try:
            value = json.loads(metadata)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProvenanceValidationError("provenance sequence is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ProvenanceValidationError("provenance sequence is not an object")
        if value.get("schema") == PROVENANCE_TERMINAL_SCHEMA:
            terminal = ProvenanceTerminalDocument.from_json_bytes(metadata)
            if (
                terminal.sequence != sequence
                or terminal.archive_generation != root.archive_generation
                or terminal.archive_tree_sha256 != root.archive_tree_sha256
            ):
                raise ProvenanceValidationError("provenance sidecar terminal is invalid")
            update_ordered_volume_commitment(ordered, terminal)
            break
        document = ProvenanceVolumeDocument.from_json_bytes(metadata)
        update_ordered_volume_commitment(ordered, document)
        if (
            document.sequence != sequence
            or document.archive_tree_sha256 != root.archive_tree_sha256
        ):
            raise ProvenanceValidationError("provenance sidecar volume sequence is invalid")
        payload = (root_dir / "payloads" / f"volume-{sequence_token}.bin").read_bytes()
        if (
            len(payload) != document.payload.bytes
            or hashlib.sha256(payload).hexdigest() != document.payload.sha256
        ):
            raise ProvenanceValidationError("provenance sidecar payload identity differs")
        if document.payload.kind == "bindings":
            first, rows = parse_binding_segment(payload)
            if first != next_file_order:
                raise ProvenanceValidationError("provenance sidecar bindings are not contiguous")
            for row in rows:
                bindings.append(_binding_from_mapping(row))
                next_file_order += 1
        sequence += 1
    if ordered.hexdigest() != root.ordered_volume_sha256:
        raise ProvenanceValidationError("provenance sidecar differs from its root")
    summaries = validate_journal_set(journals)
    directly_bound: set[str] = set()
    for binding in bindings:
        if binding.status != "captured":
            continue
        journal_id = str(binding.journal_id)
        summary = summaries.get(journal_id)
        if summary is None or binding.current_state_id != summary.current_state_id:
            raise ProvenanceValidationError(f"captured file state is unresolved: {binding.path}")
        verify_payload_binding(
            summary,
            path=binding.path,
            byte_count=binding.bytes,
            sha256=binding.sha256,
        )
        directly_bound.add(journal_id)
    reachable = set(directly_bound)
    pending = list(directly_bound)
    while pending:
        for reference in summaries[pending.pop()].external_states:
            if reference.journal_id not in reachable:
                reachable.add(reference.journal_id)
                pending.append(reference.journal_id)
    if reachable != set(summaries):
        raise ProvenanceValidationError("provenance sidecar contains an unreachable journal")
    return _ValidatedSegmentedSet(
        bindings=tuple(bindings),
        journals=summaries,
        journal_bytes=journals,
        identity=root.identity,
    )


def _binding_from_mapping(row: dict[str, object]) -> FileProvenanceBinding:
    status = row.get("status")
    if status == "captured":
        return FileProvenanceBinding(
            path=str(row["path"]),
            bytes=_required_nonnegative_int(row["bytes"], "provenance binding bytes"),
            sha256=str(row["sha256"]),
            status="captured",
            journal_id=str(row["journal_id"]),
            current_state_id=str(row["current_state_id"]),
        )
    if status == "omitted":
        return FileProvenanceBinding(
            path=str(row["path"]),
            bytes=_required_nonnegative_int(row["bytes"], "provenance binding bytes"),
            sha256=str(row["sha256"]),
            status="omitted",
            omission_reason=str(row["omission_reason"]),
        )
    raise ProvenanceValidationError("provenance sidecar binding status is invalid")


def _required_nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProvenanceValidationError(f"{label} is invalid")
    return value


def _journal_closure(
    journal_id: str,
    validated: _ValidatedSegmentedSet,
) -> dict[str, bytes]:
    reachable = {journal_id}
    pending = [journal_id]
    while pending:
        summary = validated.journals[pending.pop()]
        for reference in summary.external_states:
            if reference.journal_id not in reachable:
                reachable.add(reference.journal_id)
                pending.append(reference.journal_id)
    return {item: validated.journal_bytes[item] for item in sorted(reachable)}


def _payload_identity(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
            byte_count += len(chunk)
    return byte_count, digest.hexdigest()

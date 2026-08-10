from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .archive import (
    FileProvenanceBinding,
    ValidatedProvenanceIndex,
    validate_portable_provenance_set,
)
from .journal import (
    ProvenanceValidationError,
    append_observation,
    create_observation_journal,
    validate_journal,
)

SIDECAR_SUFFIX = ".riverhog-provenance.json-seq"


@dataclass(frozen=True, slots=True)
class PreparedFileProvenance:
    binding: FileProvenanceBinding
    journals: dict[str, bytes]
    source: str


def canonical_sidecar_path(payload: Path) -> Path:
    return payload.with_name(payload.name + SIDECAR_SUFFIX)


def prepare_file_provenance(
    payload: Path,
    *,
    relative_path: str,
    host_id: str,
    agent_name: str,
    agent_version: str,
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
        journal = create_observation_journal(
            payload,
            relative_path=relative_path,
            host_id=host_id,
            agent_name=agent_name,
            agent_version=agent_version,
        )
        summary = validate_journal(journal)
        return _captured(
            relative_path, byte_count, sha256, {summary.journal_id: journal}, "captured"
        )
    if discovered.is_dir() or discovered.name == "index.json":
        index_path = discovered / "index.json" if discovered.is_dir() else discovered
        validated = _load_portable_set(index_path)
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
        continued = append_observation(
            current,
            payload,
            relative_path=relative_path,
            host_id=host_id,
            agent_name=agent_name,
            agent_version=agent_version,
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
        index = parent / ".riverhog" / "provenance" / "index.json"
        if index.is_file():
            return index
    return None


def _load_portable_set(index_path: Path) -> ValidatedProvenanceIndex:
    root = index_path.parent
    journal_dir = root / "journals"
    journals: dict[str, bytes] = {}
    if journal_dir.is_dir():
        for path in sorted(journal_dir.glob("*.json-seq")):
            journal_id = path.name.removesuffix(".json-seq")
            journals[journal_id] = path.read_bytes()
    return validate_portable_provenance_set(index_path.read_bytes(), journals)


def _journal_closure(
    journal_id: str,
    validated: ValidatedProvenanceIndex,
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

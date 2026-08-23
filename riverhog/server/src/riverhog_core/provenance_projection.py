from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass

from riverhog_provenance import JournalSummary, validate_journal

from riverhog_core.catalog_models import (
    CollectionProvenanceEntityRecord,
    CollectionProvenanceExternalStateReferenceRecord,
)


@dataclass(frozen=True, slots=True)
class ProvenanceJournalProjection:
    summary: JournalSummary
    entities: tuple[CollectionProvenanceEntityRecord, ...]
    external_state_references: tuple[CollectionProvenanceExternalStateReferenceRecord, ...]
    entity_counts_json: str


def provenance_entity_records(
    *,
    collection_id: int,
    journal_id: str,
    content: bytes,
) -> tuple[CollectionProvenanceEntityRecord, ...]:
    """Build the disposable database projection from one exact journal."""

    return provenance_journal_projection(
        collection_id=collection_id,
        journal_id=journal_id,
        summary=validate_journal(content),
    ).entities


def provenance_journal_projection(
    *,
    collection_id: int,
    journal_id: str,
    summary: JournalSummary,
) -> ProvenanceJournalProjection:
    """Build every disposable database projection from one validated journal."""

    if summary.journal_id != journal_id:
        raise ValueError("provenance projection journal identity differs from its key")
    entities: dict[tuple[str, str], tuple[str, dict[str, object]]] = {}
    counts: Counter[str] = Counter()
    entity_lists = (
        "agents",
        "lineages",
        "states",
        "environments",
        "captures",
        "activities",
        "relations",
        "payload_bindings",
        "extensions",
    )
    for frame in summary.frames:
        entry_id = str(frame.document["id"])
        body = frame.document.get("body")
        assertions = body.get("assertions") if isinstance(body, dict) else None
        if not isinstance(assertions, dict):
            continue
        for key, values in assertions.items():
            if isinstance(values, list):
                counts[key] += len(values)
        for entity_type in entity_lists:
            values = assertions.get(entity_type, [])
            if not isinstance(values, list):
                continue
            for value in values:
                if not isinstance(value, dict) or not isinstance(value.get("id"), str):
                    continue
                entity_id = str(value["id"])
                entities[(entity_type, entity_id)] = (entry_id, value)
                if entity_type == "activities":
                    evidence = value.get("evidence", [])
                    if isinstance(evidence, list):
                        for item in evidence:
                            if isinstance(item, dict) and isinstance(item.get("id"), str):
                                entities[("evidence", str(item["id"]))] = (entry_id, item)
    entity_records = tuple(
        CollectionProvenanceEntityRecord(
            collection_id=collection_id,
            journal_id=journal_id,
            entity_type=entity_type,
            entity_id=entity_id,
            entry_id=entry_id,
            document_json=json.dumps(document, sort_keys=True, separators=(",", ":")),
        )
        for (entity_type, entity_id), (entry_id, document) in sorted(entities.items())
    )
    external_state_references = tuple(
        CollectionProvenanceExternalStateReferenceRecord(
            collection_id=collection_id,
            from_journal_id=journal_id,
            to_journal_id=reference.journal_id,
            entry_id=reference.entry_id,
            state_id=reference.state_id,
            entry_json_sha256=reference.entry_json_sha256,
        )
        for reference in sorted(
            summary.external_states,
            key=lambda item: (
                item.journal_id,
                item.entry_id,
                item.state_id,
                item.entry_json_sha256,
            ),
        )
    )
    return ProvenanceJournalProjection(
        summary=summary,
        entities=entity_records,
        external_state_references=external_state_references,
        entity_counts_json=json.dumps(dict(sorted(counts.items())), separators=(",", ":")),
    )

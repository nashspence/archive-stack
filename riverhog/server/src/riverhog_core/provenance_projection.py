from __future__ import annotations

import json

from riverhog_provenance import validate_journal

from riverhog_core.catalog_models import CollectionProvenanceEntityRecord


def provenance_entity_records(
    *,
    collection_id: int,
    journal_id: str,
    content: bytes,
) -> tuple[CollectionProvenanceEntityRecord, ...]:
    """Build the disposable database projection from one exact journal."""

    summary = validate_journal(content)
    entities: dict[tuple[str, str], tuple[str, dict[str, object]]] = {}
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
    return tuple(
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

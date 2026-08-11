from __future__ import annotations

import hashlib
import json
from pathlib import Path

from riverhog_core.app_permissions import (
    ALL_RESOURCES,
    CATALOG_READ,
    PROVENANCE_READ,
    ApplicationAccess,
    ApplicationPrincipal,
)
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionFileProvenanceRecord,
    CollectionFileRecord,
    CollectionProvenanceJournalRecord,
    CollectionRecord,
)
from riverhog_core.provenance_projection import provenance_journal_projection
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.provenance import SqlAlchemyProvenanceService
from riverhog_provenance import (
    create_derivative_journal,
    create_observation_journal,
    validate_journal,
)

from tests.unit.db_helpers import sqlite_url

NOW = "2026-01-01T00:00:00.000000Z"
READER = ApplicationPrincipal(
    app="reader",
    key_id="reader-key",
    access=frozenset(
        {
            ApplicationAccess(CATALOG_READ, ALL_RESOURCES),
            ApplicationAccess(PROVENANCE_READ, ALL_RESOURCES),
        }
    ),
)


def _payload(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


def test_trace_reads_only_reachable_validated_lineage_projection(
    tmp_path: Path,
) -> None:
    first = create_observation_journal(
        _payload(tmp_path / "first.mov", b"first"),
        relative_path="first.mov",
        host_id="urn:uuid:00000000-0000-4000-8000-000000000001",
        agent_name="fixture",
        agent_version="1.0.0",
    )
    second = create_observation_journal(
        _payload(tmp_path / "second.mov", b"second"),
        relative_path="second.mov",
        host_id="urn:uuid:00000000-0000-4000-8000-000000000002",
        agent_name="fixture",
        agent_version="1.0.0",
    )
    derivative_path = _payload(tmp_path / "derivative.tar", b"derivative")
    derivative = create_derivative_journal(
        derivative_path,
        relative_path="derivative.tar",
        source_journals=(first, second),
        host_id="urn:uuid:00000000-0000-4000-8000-000000000003",
        agent_name="fixture",
        agent_version="1.0.0",
        event_label="Fixture aggregation",
        started_at=NOW,
        ended_at=NOW,
        derivation_kind="aggregation",
    )
    unrelated = create_observation_journal(
        _payload(tmp_path / "unrelated.bin", b"unrelated"),
        relative_path="unrelated.bin",
        host_id="urn:uuid:00000000-0000-4000-8000-000000000004",
        agent_name="fixture",
        agent_version="1.0.0",
    )
    journals = (first, second, derivative, unrelated)
    summaries = {
        validate_journal(content).journal_id: validate_journal(content) for content in journals
    }
    contents = {validate_journal(content).journal_id: content for content in journals}

    database_url = sqlite_url(tmp_path / "catalog.sqlite3")
    initialize_db(database_url)
    with session_scope(make_session_factory(database_url)) as session:
        session.add(
            CollectionRecord(
                id=1,
                creation_idempotency_key="fixture",
                content_etag="a" * 64,
                provenance_mode="captured",
                provenance_etag="b" * 64,
                record_etag="c" * 64,
                metadata_revision=1,
                metadata_updated_at=NOW,
                created_by_app="fixture",
                created_at=NOW,
            )
        )
        projections = {}
        for journal_id, summary in summaries.items():
            projection = provenance_journal_projection(
                collection_id=1,
                journal_id=journal_id,
                summary=summary,
            )
            projections[journal_id] = projection
            session.add(
                CollectionProvenanceJournalRecord(
                    collection_id=1,
                    journal_id=journal_id,
                    journal_bytes=contents[journal_id],
                    bytes=len(contents[journal_id]),
                    sha256=summary.journal_sha256,
                    entries=len(summary.frames),
                    agent_ids_json=json.dumps(sorted(summary.agent_ids), separators=(",", ":")),
                    entity_counts_json=projection.entity_counts_json,
                    current_state_id=summary.current_state_id,
                    current_path=summary.current_path,
                    current_bytes=summary.current_bytes,
                    current_sha256=summary.current_sha256,
                )
            )
        session.flush()
        for projection in projections.values():
            session.add_all(projection.lineage_edges)
        file_bindings = (
            (derivative_path.name, derivative_path.read_bytes(), validate_journal(derivative)),
            ("unrelated.bin", b"unrelated", validate_journal(unrelated)),
        )
        for path, content, _summary in file_bindings:
            sha256 = hashlib.sha256(content).hexdigest()
            session.add(
                CollectionFileRecord(
                    collection_id=1,
                    path=path,
                    bytes=len(content),
                    sha256=sha256,
                )
            )
        session.flush()
        for path, _content, summary in file_bindings:
            session.add(
                CollectionFileProvenanceRecord(
                    collection_id=1,
                    path=path,
                    status="captured",
                    journal_id=summary.journal_id,
                    current_state_id=summary.current_state_id,
                    omission_reason=None,
                )
            )

    service = SqlAlchemyProvenanceService(RuntimeConfig(database_url=database_url))
    traced = service.trace_file(1, "derivative.tar", principal=READER)

    derivative_summary = validate_journal(derivative)
    source_ids = {validate_journal(first).journal_id, validate_journal(second).journal_id}
    assert {item["journal_id"] for item in traced["journals"]} == {
        derivative_summary.journal_id,
        *source_ids,
    }
    assert {item["to_journal_id"] for item in traced["lineage_edges"]} == source_ids
    assert validate_journal(unrelated).journal_id not in {
        item["journal_id"] for item in traced["journals"]
    }

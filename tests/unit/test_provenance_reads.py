from __future__ import annotations

import hashlib
from pathlib import Path

from riverhog_core.app_permissions import (
    ALL_RESOURCES,
    CATALOG_READ,
    PROVENANCE_EXPORT,
    PROVENANCE_READ,
    ApplicationAccess,
    ApplicationPrincipal,
)
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionFileProvenanceRecord,
    CollectionFileRecord,
    CollectionProvenanceJournalAgentRecord,
    CollectionProvenanceJournalChunkRecord,
    CollectionProvenanceJournalRecord,
    CollectionProvenanceVerificationRecord,
    CollectionRecord,
)
from riverhog_core.provenance_projection import provenance_journal_projection
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services import provenance as provenance_module
from riverhog_core.services.provenance import SqlAlchemyProvenanceService
from riverhog_provenance import (
    create_derivative_journal,
    create_observation_journal,
    current_state_reference,
    validate_journal,
)

from tests.provenance_observer import native_provenance_observer
from tests.unit.artifact_scope_fixtures import persisted_artifact_scope
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


def _omitted_provenance_service(tmp_path: Path) -> SqlAlchemyProvenanceService:
    database_url = sqlite_url(tmp_path / "verification.sqlite3")
    initialize_db(database_url)
    with session_scope(make_session_factory(database_url)) as session:
        session.add(
            CollectionRecord(
                id=1,
                creation_idempotency_key="verification-fixture",
                creation_identity_sha256="e" * 64,
                creation_custody_mode="producer-retained",
                content_identity="a" * 64,
                encryption_format="age-v1-scrypt",
                passphrase_id="fixture-archive-key-v1",
                provenance_mode="omitted",
                provenance_identity=None,
                inventory_identity="c" * 64,
                created_by_app="fixture",
                created_at=NOW,
                file_count=0,
                file_bytes=0,
            )
        )
    return SqlAlchemyProvenanceService(RuntimeConfig(database_url=database_url))


def test_trace_reads_only_reachable_validated_lineage_projection(
    tmp_path: Path,
) -> None:
    first = create_observation_journal(
        _payload(tmp_path / "first.mov", b"first"),
        relative_path="first.mov",
        host_id="urn:uuid:00000000-0000-4000-8000-000000000001",
        agent_name="fixture",
        agent_version="1.0.0",
        observer=native_provenance_observer(),
    )
    second = create_observation_journal(
        _payload(tmp_path / "second.mov", b"second"),
        relative_path="second.mov",
        host_id="urn:uuid:00000000-0000-4000-8000-000000000002",
        agent_name="fixture",
        agent_version="1.0.0",
        observer=native_provenance_observer(),
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
        observer=native_provenance_observer(),
        derivation_kind="aggregation",
    )
    unrelated = create_observation_journal(
        _payload(tmp_path / "unrelated.bin", b"unrelated"),
        relative_path="unrelated.bin",
        host_id="urn:uuid:00000000-0000-4000-8000-000000000004",
        agent_name="fixture",
        agent_version="1.0.0",
        observer=native_provenance_observer(),
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
                creation_identity_sha256="e" * 64,
                creation_custody_mode="producer-retained",
                content_identity="a" * 64,
                encryption_format="age-v1-scrypt",
                passphrase_id="fixture-archive-key-v1",
                provenance_mode="captured",
                provenance_identity="b" * 64,
                inventory_identity="c" * 64,
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
            current = current_state_reference(contents[journal_id])
            session.add(
                CollectionProvenanceJournalRecord(
                    collection_id=1,
                    journal_id=journal_id,
                    bytes=len(contents[journal_id]),
                    sha256=summary.journal_sha256,
                    entries=len(summary.frames),
                    agent_count=len(summary.agent_ids),
                    entity_counts_json=projection.entity_counts_json,
                    current_state_id=summary.current_state_id,
                    current_entry_id=current.entry_id,
                    current_entry_json_sha256=current.entry_json_sha256,
                    current_path=summary.current_path,
                    current_bytes=summary.current_bytes,
                    current_sha256=summary.current_sha256,
                )
            )
            session.flush()
            session.add_all(
                CollectionProvenanceJournalAgentRecord(
                    collection_id=1,
                    journal_id=journal_id,
                    agent_id=agent_id,
                )
                for agent_id in summary.agent_ids
            )
            session.add(
                CollectionProvenanceJournalChunkRecord(
                    collection_id=1,
                    journal_id=journal_id,
                    ordinal=0,
                    byte_offset=0,
                    content=contents[journal_id],
                )
            )
        session.flush()
        for projection in projections.values():
            session.add_all(projection.external_state_references)
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
                    provenance_status="captured",
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
    traced = service.trace_file(
        1,
        "derivative.tar",
        page_size=100,
        position=None,
        principal=READER,
    )

    derivative_summary = validate_journal(derivative)
    source_ids = {validate_journal(first).journal_id, validate_journal(second).journal_id}
    assert {
        item["journal"]["journal_id"] for item in traced["items"] if item["kind"] == "journal"
    } == {
        derivative_summary.journal_id,
        *source_ids,
    }
    assert {
        item["reference"]["to_journal_id"]
        for item in traced["items"]
        if item["kind"] == "external_state_reference"
    } == source_ids
    assert validate_journal(unrelated).journal_id not in {
        item["journal"]["journal_id"] for item in traced["items"] if item["kind"] == "journal"
    }
    streamed_trace = list(service.iter_trace_file(1, "derivative.tar", principal=READER))
    assert streamed_trace == traced["items"]
    bounded_pages = []
    position = None
    while True:
        current = service.trace_file(
            1,
            "derivative.tar",
            page_size=2,
            position=position,
            principal=READER,
        )
        bounded_pages.append(current)
        position = current["_next_position"]
        if position is None:
            break
    assert [item for current in bounded_pages for item in current["items"]] == streamed_trace
    assert len(bounded_pages) == 3

    scoped = persisted_artifact_scope(
        database_url,
        access=(
            ApplicationAccess(CATALOG_READ, "collection:1"),
            ApplicationAccess(PROVENANCE_READ, "collection:1"),
            ApplicationAccess(PROVENANCE_EXPORT, "collection:1"),
        ),
        artifacts=(
            (
                1,
                "derivative.tar",
                derivative_path.stat().st_size,
                hashlib.sha256(derivative_path.read_bytes()).hexdigest(),
            ),
        ),
    )
    listed = list(
        service.iter_files(
            1,
            q=None,
            status=None,
            sort="path",
            order="asc",
            principal=scoped,
        )
    )
    assert [item["path"] for item in listed] == ["derivative.tar"]
    scoped_trace = service.trace_file(
        1,
        "derivative.tar",
        page_size=100,
        position=None,
        principal=scoped,
    )
    assert {
        item["journal"]["journal_id"] for item in scoped_trace["items"] if item["kind"] == "journal"
    } == {
        derivative_summary.journal_id,
        *source_ids,
    }
    exported = b"".join(
        service.iter_journal(
            1,
            validate_journal(first).journal_id,
            principal=scoped,
        )
    )
    assert exported == first


def test_provenance_verification_job_is_restartable_and_cancellable(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    service = _omitted_provenance_service(tmp_path)

    queued = service.request_verification(1, principal=READER)
    assert queued["state"] == "queued"
    canceled = service.cancel_verification(1, principal=READER)
    assert canceled["state"] == "canceled"

    retried = service.request_verification(1, principal=READER)
    assert retried["state"] == "queued"

    def interrupt(collection_id: int) -> dict[str, object]:
        assert collection_id == 1
        assert service.cancel_verification(1, principal=READER)["state"] == "canceling"
        raise provenance_module._VerificationCanceled

    monkeypatch.setattr(service, "_advance_verification", interrupt)
    assert service.process_due_verifications() == 1
    assert service.get_verification(1, principal=READER)["state"] == "canceled"

    service.request_verification(1, principal=READER)
    with session_scope(service._session_factory) as session:
        record = session.get(CollectionProvenanceVerificationRecord, 1)
        assert record is not None
        record.state = "running"
        record.started_at = NOW
    assert service.requeue_interrupted_verifications_for_startup() == 1
    restarted = service.get_verification(1, principal=READER)
    assert restarted["state"] == "queued"
    assert restarted["started_at"] is None


def test_provenance_verification_job_persists_terminal_result(tmp_path: Path) -> None:
    service = _omitted_provenance_service(tmp_path)

    service.request_verification(1, principal=READER)
    assert service.process_due_verifications() == 1

    completed = service.get_verification(1, principal=READER)
    assert completed["state"] == "succeeded"
    assert completed["attempts"] == 1
    assert completed["result"] == {
        "collection_id": 1,
        "valid": True,
        "provenance_mode": "omitted",
        "provenance_identity": None,
        "files": 0,
        "journals": 0,
        "entities": 0,
    }

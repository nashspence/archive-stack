from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from riverhog_provenance import (
    ProvenanceValidationError,
    append_observation,
    append_replacement_transformation,
    create_derivative_journal,
    create_derivative_journal_from_identity,
    create_observation_journal,
    validate_journal,
    validate_journal_set,
    verify_payload_binding,
)

from tests.provenance_observer import native_provenance_observer


def _payload(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    path.chmod(0o640)
    return path


def test_observation_journal_binds_payload_and_continues_exact_prefix(
    tmp_path: Path, urn_factory
) -> None:
    payload = _payload(tmp_path / "source.bin", b"source bytes")
    original = create_observation_journal(
        payload,
        relative_path="source.bin",
        host_id=urn_factory(),
        agent_name="riverhog-client",
        agent_version="0.1.0",
        observer=native_provenance_observer(),
    )
    initial = validate_journal(original)
    verify_payload_binding(
        initial,
        path="source.bin",
        byte_count=len(b"source bytes"),
        sha256=hashlib.sha256(b"source bytes").hexdigest(),
    )

    continued = append_observation(
        original,
        payload,
        relative_path="staged/source.bin",
        host_id=urn_factory(),
        agent_name="stove0",
        agent_version="0.1.0",
        observer=native_provenance_observer(),
    )
    current = validate_journal(continued)

    assert continued.startswith(original)
    assert current.journal_id == initial.journal_id
    assert current.primary_lineage_id == initial.primary_lineage_id
    assert current.current_path == "staged/source.bin"
    assert len(current.frames) == 3


def test_replacement_transformation_stays_in_the_same_lineage(tmp_path: Path, urn_factory) -> None:
    source = _payload(tmp_path / "source.mov", b"source container")
    journal = create_observation_journal(
        source,
        relative_path="source.mov",
        host_id=urn_factory(),
        agent_name="target-client",
        agent_version="0.1.0",
        observer=native_provenance_observer(),
    )
    initial = validate_journal(journal)
    output = _payload(tmp_path / "source.mkv", b"transcoded container")

    transformed = append_replacement_transformation(
        journal,
        output,
        relative_path="source.mkv",
        host_id=urn_factory(),
        agent_name="target",
        agent_version="0.1.0",
        event_label="Canonical transcode",
        started_at="2026-08-10T01:00:00Z",
        ended_at="2026-08-10T01:01:00Z",
        observer=native_provenance_observer(),
    )
    current = validate_journal(transformed)

    assert transformed.startswith(journal)
    assert current.primary_lineage_id == initial.primary_lineage_id
    assert current.current_state_id != initial.current_state_id
    assert current.current_path == "source.mkv"
    assert current.current_sha256 == hashlib.sha256(b"transcoded container").hexdigest()


def test_derivative_gets_a_new_lineage_with_exact_multi_input_references(
    tmp_path: Path, urn_factory
) -> None:
    sources: list[bytes] = []
    for name, content in (("video.mov", b"video"), ("captions.srt", b"captions")):
        path = _payload(tmp_path / name, content)
        sources.append(
            create_observation_journal(
                path,
                relative_path=name,
                host_id=urn_factory(),
                agent_name="target-client",
                agent_version="0.1.0",
                observer=native_provenance_observer(),
            )
        )
    output = _payload(tmp_path / "source-artifacts.tar.zst", b"artifacts")

    derivative = create_derivative_journal(
        output,
        relative_path="video/source-artifacts.tar.zst",
        source_journals=sources,
        host_id=urn_factory(),
        agent_name="target",
        agent_version="0.1.0",
        event_label="Preserve source artifacts",
        started_at="2026-08-10T01:00:00Z",
        ended_at="2026-08-10T01:01:00Z",
        observer=native_provenance_observer(),
        derivation_kind="aggregation",
    )
    summaries = validate_journal_set(
        {
            **{validate_journal(item).journal_id: item for item in sources},
            validate_journal(derivative).journal_id: derivative,
        }
    )
    current = validate_journal(derivative)

    assert current.primary_lineage_id not in {
        validate_journal(item).primary_lineage_id for item in sources
    }
    assert len(current.external_states) == 2
    assert set(summaries) == {
        current.journal_id,
        *(validate_journal(item).journal_id for item in sources),
    }


def test_identity_derivative_binds_stream_without_rereading_payload(
    tmp_path: Path, urn_factory
) -> None:
    source_path = _payload(tmp_path / "source.bin", b"source")
    source = create_observation_journal(
        source_path,
        relative_path="source.bin",
        host_id=urn_factory(),
        agent_name="riverhog-client",
        agent_version="0.1.0",
        observer=native_provenance_observer(),
    )
    output = b"generated range-readable output"
    journal_id = urn_factory()

    derivative = create_derivative_journal_from_identity(
        relative_path="derived/output.bin",
        byte_count=len(output),
        sha256=hashlib.sha256(output).hexdigest(),
        source_journals=(source,),
        agent_name="fixture-target",
        agent_version="0.1.0",
        event_label="fixture.operation/v1",
        started_at="2026-08-10T01:00:00Z",
        ended_at="2026-08-10T01:01:00Z",
        journal_id=journal_id,
    )
    summary = validate_journal(derivative)

    assert summary.journal_id == journal_id
    assert summary.current_path == "derived/output.bin"
    assert summary.current_bytes == len(output)
    assert summary.current_sha256 == hashlib.sha256(output).hexdigest()
    source_summary = validate_journal(source)
    assert len(summary.external_states) == 1
    assert summary.external_states[0].journal_id == source_summary.journal_id
    validate_journal_set(
        {
            source_summary.journal_id: source,
            journal_id: derivative,
        }
    )


def test_payload_binding_mismatch_is_rejected(tmp_path: Path, urn_factory) -> None:
    payload = _payload(tmp_path / "source.bin", b"source bytes")
    summary = validate_journal(
        create_observation_journal(
            payload,
            relative_path="source.bin",
            host_id=urn_factory(),
            agent_name="riverhog-client",
            agent_version="0.1.0",
            observer=native_provenance_observer(),
        )
    )

    with pytest.raises(ProvenanceValidationError, match="does not bind"):
        verify_payload_binding(
            summary,
            path="source.bin",
            byte_count=len(b"different"),
            sha256=hashlib.sha256(b"different").hexdigest(),
        )


def test_noncanonical_or_truncated_journal_is_rejected(tmp_path: Path, urn_factory) -> None:
    payload = _payload(tmp_path / "source.bin", b"source bytes")
    journal = create_observation_journal(
        payload,
        relative_path="source.bin",
        host_id=urn_factory(),
        agent_name="riverhog-client",
        agent_version="0.1.0",
        observer=native_provenance_observer(),
    )

    with pytest.raises(ProvenanceValidationError):
        validate_journal(journal.rstrip(b"\n"))

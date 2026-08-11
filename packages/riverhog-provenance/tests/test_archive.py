from __future__ import annotations

from pathlib import Path

import pytest
from riverhog_provenance import (
    FileProvenanceBinding,
    ProvenanceValidationError,
    build_portable_provenance_set,
    build_provenance_archive,
    create_observation_journal,
    prepare_file_provenance,
    validate_journal,
    validate_portable_provenance_set,
    validate_provenance_archive,
)


def _journal(tmp_path: Path, urn_factory) -> tuple[Path, bytes]:
    payload = tmp_path / "movie.bin"
    payload.write_bytes(b"original payload")
    content = create_observation_journal(
        payload,
        relative_path="movie.bin",
        host_id=urn_factory(),
        agent_name="riverhog-test",
        agent_version="0.1.0",
    )
    return payload, content


def test_archive_index_and_bundle_round_trip_deterministically(tmp_path: Path, urn_factory) -> None:
    payload, journal = _journal(tmp_path, urn_factory)
    summary = validate_journal(journal)
    bindings = (
        FileProvenanceBinding(
            path="movie.bin",
            bytes=payload.stat().st_size,
            sha256=summary.current_sha256,
            status="captured",
            journal_id=summary.journal_id,
            current_state_id=summary.current_state_id,
        ),
        FileProvenanceBinding(
            path="notes.txt",
            bytes=4,
            sha256="0" * 64,
            status="omitted",
            omission_reason="source application supplied no provenance",
        ),
    )
    journals = {summary.journal_id: journal}

    first = build_provenance_archive(bindings=bindings, journals=journals)
    second = build_provenance_archive(bindings=bindings, journals=journals)

    assert first == second
    assert first.bundles[0].relative_path == "provenance/bundle-000000000000.tar.age"
    assert first.journal_summaries == {summary.journal_id: summary}
    validated = validate_provenance_archive(
        first.index_bytes,
        {first.bundles[0].bundle_id: first.bundles[0].content},
    )
    assert validated.identity == first.identity
    assert validated.journal_bytes == journals
    assert validated.bindings == bindings


def test_portable_recovery_set_round_trip(tmp_path: Path, urn_factory) -> None:
    payload, journal = _journal(tmp_path, urn_factory)
    summary = validate_journal(journal)
    binding = FileProvenanceBinding(
        path="movie.bin",
        bytes=payload.stat().st_size,
        sha256=summary.current_sha256,
        status="captured",
        journal_id=summary.journal_id,
        current_state_id=summary.current_state_id,
    )
    index = build_portable_provenance_set(
        bindings=(binding,), journals={summary.journal_id: journal}
    )

    validated = validate_portable_provenance_set(index, {summary.journal_id: journal})

    assert validated.bindings == (binding,)
    assert validated.journals[summary.journal_id].current_state_id == summary.current_state_id


def test_archive_rejects_a_payload_binding_mismatch(tmp_path: Path, urn_factory) -> None:
    _, journal = _journal(tmp_path, urn_factory)
    summary = validate_journal(journal)
    binding = FileProvenanceBinding(
        path="movie.bin",
        bytes=summary.current_bytes + 1,
        sha256=summary.current_sha256,
        status="captured",
        journal_id=summary.journal_id,
        current_state_id=summary.current_state_id,
    )

    with pytest.raises(ProvenanceValidationError, match="does not bind"):
        build_provenance_archive(bindings=(binding,), journals={summary.journal_id: journal})


def test_existing_sidecar_is_preserved_as_an_exact_prefix(tmp_path: Path, urn_factory) -> None:
    payload, journal = _journal(tmp_path, urn_factory)
    sidecar = payload.with_name(payload.name + ".riverhog-provenance.json-seq")
    sidecar.write_bytes(journal)

    prepared = prepare_file_provenance(
        payload,
        relative_path="renamed/movie.bin",
        host_id=urn_factory(),
        agent_name="riverhog-client",
        agent_version="0.1.0",
    )

    continued = prepared.journals[str(prepared.binding.journal_id)]
    assert continued.startswith(journal)
    assert prepared.binding.path == "renamed/movie.bin"
    assert prepared.source == "continued"


def test_explicit_omission_is_accounted_for(tmp_path: Path, urn_factory) -> None:
    payload = tmp_path / "movie.bin"
    payload.write_bytes(b"payload")

    prepared = prepare_file_provenance(
        payload,
        relative_path="movie.bin",
        host_id=urn_factory(),
        agent_name="riverhog-client",
        agent_version="0.1.0",
        omit_reason="operator deliberately omitted source provenance",
    )

    assert prepared.binding.status == "omitted"
    assert prepared.journals == {}

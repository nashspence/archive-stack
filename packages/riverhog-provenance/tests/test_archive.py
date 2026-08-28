from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
import riverhog_provenance.schema as provenance_schema
import riverhog_provenance_linux_contracts as linux_contracts
import riverhog_provenance_macos_contracts as macos_contracts
import riverhog_provenance_windows_contracts as windows_contracts
from jsonschema import Draft202012Validator
from riverhog_provenance import (
    FileProvenanceBinding,
    ProvenanceValidationError,
    build_portable_provenance_set,
    build_provenance_archive,
    create_observation_journal,
    load_provenance_index_schema,
    load_provenance_set_schema,
    prepare_file_provenance,
    validate_journal,
    validate_portable_provenance_set,
    validate_provenance_archive,
)
from riverhog_provenance_contracts import PROVENANCE_SCHEMA_FORMAT_POLICY

from tests.provenance_observer import native_provenance_observer


def _all_json_nodes(value: object) -> Iterator[object]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _all_json_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _all_json_nodes(child)


def _journal(tmp_path: Path, urn_factory) -> tuple[Path, bytes]:
    payload = tmp_path / "movie.bin"
    payload.write_bytes(b"original payload")
    content = create_observation_journal(
        payload,
        relative_path="movie.bin",
        host_id=urn_factory(),
        agent_name="riverhog-test",
        agent_version="0.1.0",
        observer=native_provenance_observer(),
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


def test_published_archive_index_schemas_are_valid_and_collection_unbounded() -> None:
    schemas = (load_provenance_index_schema(), load_provenance_set_schema())

    for schema in schemas:
        Draft202012Validator.check_schema(schema)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert all(
            "maxItems" not in node for node in _all_json_nodes(schema) if isinstance(node, dict)
        )


def test_every_builtin_provenance_schema_uses_the_shared_annotation_only_profile() -> None:
    assert PROVENANCE_SCHEMA_FORMAT_POLICY == "annotation-only"
    validators = (
        provenance_schema._journal_entry_validator(),
        provenance_schema._graph_fragment_validator(),
        provenance_schema._provenance_index_validator(),
        provenance_schema._provenance_set_validator(),
        *provenance_schema._observer_validators().values(),
    )

    assert all(validator.format_checker is None for validator in validators)


def _write_duplicate_schema_pack(path: Path) -> None:
    document = {"$id": "https://riverhog.example/duplicate/v1", "type": "object"}
    for name in ("first.schema.json", "second.schema.json"):
        path.joinpath(name).write_text(json.dumps(document), encoding="utf-8")


def test_portable_observer_schema_pack_rejects_duplicate_identities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_duplicate_schema_pack(tmp_path)
    monkeypatch.setattr(provenance_schema, "_schema_directory", lambda: tmp_path)

    with pytest.raises(ValueError, match="duplicated"):
        provenance_schema.load_observer_schemas()


@pytest.mark.parametrize(
    "contracts",
    (linux_contracts, macos_contracts, windows_contracts),
)
def test_platform_observer_schema_packs_reject_duplicate_identities(
    contracts: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema_directory = tmp_path / "schemas"
    schema_directory.mkdir()
    _write_duplicate_schema_pack(schema_directory)
    monkeypatch.setattr(contracts.resources, "files", lambda _package: tmp_path)

    with pytest.raises(ValueError, match="duplicated"):
        contracts.load_schemas()


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
        observer=native_provenance_observer(),
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
        observer=native_provenance_observer(),
        omit_reason="operator deliberately omitted source provenance",
    )

    assert prepared.binding.status == "omitted"
    assert prepared.journals == {}

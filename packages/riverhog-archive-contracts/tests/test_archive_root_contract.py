from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from riverhog_archive_contracts import (
    ArchiveManifestError,
    CollectionArchiveManifest,
    CollectionArchiveTerminalDocument,
    CollectionArchiveVolumeDocument,
    CollectionTreeIdentity,
    PackArchiveVolume,
    StoredPartIdentity,
    format_archive_sequence,
    ordered_archive_volume_commitment,
)

ZERO = "0" * 64
ONE = "1" * 64


def _volume_mapping() -> dict[str, object]:
    return {
        "schema": "collection-archive-volume/v1",
        "archive_generation": ONE,
        "archive_tree_sha256": ZERO,
        "volume": {
            "id": f"pack-{format_archive_sequence(0)}",
            "sequence": format_archive_sequence(0),
            "kind": "pack",
            "path": f"volumes/pack-{format_archive_sequence(0)}.tar.age",
            "files": 1,
            "source_bytes": 0,
            "plaintext_bytes": 0,
            "age_state": {
                "format": "age-v1-scrypt-resumable",
                "header_b64": "YQ",
                "payload_nonce_b64": "MDAwMDAwMDAwMDAwMDAwMA",
                "plaintext_size": 0,
            },
            "index_sha256": ZERO,
            "plan_sha256": ONE,
            "parts": [
                {
                    "number": 1,
                    "plaintext_start": 0,
                    "plaintext_bytes": 0,
                    "plaintext_sha256": ZERO,
                    "stored_bytes": 1,
                    "stored_sha256": ONE,
                }
            ],
        },
    }


def _manifest_mapping() -> dict[str, object]:
    volume = CollectionArchiveVolumeDocument.from_mapping(_volume_mapping())
    terminal = CollectionArchiveTerminalDocument(
        archive_generation=ONE,
        archive_tree_sha256=ZERO,
        sequence=1,
    )
    return {
        "schema": "collection-archive-manifest/v1",
        "archive_generation": ONE,
        "format": {
            "encryption": "age-v1-scrypt",
            "pack_index": "riverhog-pack-index/v1",
            "part_digest": "sha256",
            "selective_read": "age-chunk-range/v1",
        },
        "tree": {"files": 1, "bytes": 0, "sha256": ZERO},
        "volume_sequence": {
            "sha256": ordered_archive_volume_commitment((volume, terminal)),
        },
    }


def test_archive_root_has_one_canonical_public_model() -> None:
    source = _manifest_mapping()
    manifest = CollectionArchiveManifest.from_mapping(source)
    reparsed = CollectionArchiveManifest.from_json_bytes(manifest.to_json_bytes())

    assert reparsed == manifest
    assert reparsed.to_mapping() == source
    assert reparsed.ordered_volume_sha256 == source["volume_sequence"]["sha256"]


def test_checked_schema_names_the_same_archive_root_contract() -> None:
    path = Path(__file__).parents[1] / "schemas" / "collection-archive-manifest-v1.schema.json"
    schema = json.loads(path.read_text())

    assert schema["properties"]["schema"]["const"] == "collection-archive-manifest/v1"
    assert schema["additionalProperties"] is False
    assert "CollectionArchiveManifest" in schema["$comment"]
    Draft202012Validator(schema).validate(_manifest_mapping())


def test_checked_volume_schema_names_the_same_bounded_volume_contract() -> None:
    path = Path(__file__).parents[1] / "schemas" / "collection-archive-volume-v1.schema.json"
    schema = json.loads(path.read_text())

    assert schema["properties"]["schema"]["const"] == "collection-archive-volume/v1"
    assert schema["additionalProperties"] is False
    assert "CollectionArchiveVolumeDocument" in schema["$comment"]
    Draft202012Validator(schema).validate(_volume_mapping())


def test_public_archive_root_constructors_share_the_parser_validity_domain() -> None:
    manifest = CollectionArchiveManifest.from_mapping(_manifest_mapping())
    volume = CollectionArchiveVolumeDocument.from_mapping(_volume_mapping()).volume
    assert isinstance(volume, PackArchiveVolume)

    with pytest.raises(ArchiveManifestError, match="tree files"):
        CollectionTreeIdentity(files=0, bytes=0, sha256=ZERO)
    with pytest.raises(ArchiveManifestError, match="part number"):
        StoredPartIdentity(
            number=0,
            plaintext_start=0,
            plaintext_bytes=0,
            plaintext_sha256=ZERO,
            stored_bytes=1,
            stored_sha256=ONE,
        )
    with pytest.raises(ArchiveManifestError, match="path is not canonical"):
        replace(volume, path="volumes/wrong.tar.age")
    with pytest.raises(ArchiveManifestError, match="part order"):
        replace(volume, parts=(replace(volume.parts[0], number=2),))

    assert CollectionArchiveManifest.from_json_bytes(manifest.to_json_bytes()) == manifest


def test_archive_root_schema_projects_expressible_semantic_constraints() -> None:
    path = Path(__file__).parents[1] / "schemas" / "collection-archive-manifest-v1.schema.json"
    validator = Draft202012Validator(json.loads(path.read_text()))
    invalid = _volume_mapping()
    volume = CollectionArchiveVolumeDocument.from_mapping(invalid)
    invalid_root = _manifest_mapping()
    volume_sequence = invalid_root["volume_sequence"]
    assert isinstance(volume_sequence, dict)
    volume_sequence["sha256"] = "invalid"

    assert volume.volume.sequence == 0
    assert list(validator.iter_errors(invalid_root))

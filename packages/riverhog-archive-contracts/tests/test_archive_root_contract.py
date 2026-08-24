from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from riverhog_archive_contracts import (
    ArchiveManifestError,
    CollectionArchiveManifest,
    CollectionTreeIdentity,
    PackArchiveVolume,
    StoredPartIdentity,
)

ZERO = "0" * 64
ONE = "1" * 64


def _manifest_mapping() -> dict[str, object]:
    return {
        "schema": "collection-archive-manifest/v1",
        "format": {
            "encryption": "age-v1-scrypt",
            "pack_index": "riverhog-pack-index/v1",
            "part_digest": "sha256",
            "selective_read": "age-chunk-range/v1",
        },
        "tree": {"files": 1, "bytes": 0, "sha256": ZERO},
        "volumes": [
            {
                "id": "pack-000000000000",
                "sequence": 0,
                "kind": "pack",
                "path": "volumes/pack-000000000000.tar.age",
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
            }
        ],
    }


def test_archive_root_has_one_canonical_public_model() -> None:
    source = _manifest_mapping()
    manifest = CollectionArchiveManifest.from_mapping(source)
    reparsed = CollectionArchiveManifest.from_json_bytes(manifest.to_json_bytes())

    assert reparsed == manifest
    assert reparsed.to_mapping() == source
    assert isinstance(reparsed.volumes[0], PackArchiveVolume)


def test_checked_schema_names_the_same_archive_root_contract() -> None:
    path = Path(__file__).parents[1] / "schemas" / "collection-archive-manifest-v1.schema.json"
    schema = json.loads(path.read_text())

    assert schema["properties"]["schema"]["const"] == "collection-archive-manifest/v1"
    assert schema["additionalProperties"] is False
    assert "CollectionArchiveManifest" in schema["$comment"]
    Draft202012Validator(schema).validate(_manifest_mapping())


def test_public_archive_root_constructors_share_the_parser_validity_domain() -> None:
    manifest = CollectionArchiveManifest.from_mapping(_manifest_mapping())
    volume = manifest.volumes[0]
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
    invalid = _manifest_mapping()
    volumes = invalid["volumes"]
    assert isinstance(volumes, list) and isinstance(volumes[0], dict)
    age_state = volumes[0]["age_state"]
    assert isinstance(age_state, dict)
    age_state["payload_nonce_b64"] = "not-a-16-byte-nonce"

    assert list(validator.iter_errors(invalid))

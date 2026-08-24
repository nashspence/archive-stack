from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from riverhog_protocol import (
    PortableCollectionError,
    PortableCollectionFile,
    PortableCollectionRecord,
    portable_collection_json_schema,
)


def test_portable_collection_model_owns_canonical_document_and_identity() -> None:
    record = PortableCollectionRecord.create(
        collection=7,
        content_identity="a" * 64,
        encryption_format="age-v1-scrypt",
        passphrase_id="collection-test-key-v1",
        provenance_mode="omitted",
        provenance_identity=None,
        metadata_revision=2,
        tags=("video", "archive"),
        files=(("z.txt", 2, "b" * 64), ("a.txt", 1, "c" * 64)),
    )

    assert record.tags == ("archive", "video")
    assert [item.path for item in record.files] == ["a.txt", "z.txt"]
    assert PortableCollectionRecord.from_json_bytes(record.to_json_bytes()) == record
    assert len(record.identity) == 64


def test_portable_collection_schema_names_the_public_model() -> None:
    path = Path(__file__).parents[1] / "schemas" / "riverhog-collection-v1.schema.json"
    schema = json.loads(path.read_text())

    assert schema["properties"]["format"]["const"] == "riverhog-collection/v1"
    assert schema["additionalProperties"] is False
    assert "PortableCollectionRecord" in schema["$comment"]
    assert portable_collection_json_schema() == schema


def test_portable_collection_factory_cannot_emit_a_parser_invalid_file() -> None:
    with pytest.raises(PortableCollectionError, match="path is not canonical"):
        PortableCollectionRecord.create(
            collection=7,
            content_identity="a" * 64,
            encryption_format="age-v1-scrypt",
            passphrase_id="collection-test-key-v1",
            provenance_mode="omitted",
            provenance_identity=None,
            metadata_revision=2,
            tags=("archive",),
            files=(("./not-canonical", 1, "b" * 64),),
        )
    with pytest.raises(PortableCollectionError, match="file bytes"):
        PortableCollectionFile(path="valid.txt", bytes=-1, sha256="b" * 64)


def test_portable_collection_schema_projects_expressible_semantic_constraints() -> None:
    schema_path = Path(__file__).parents[1] / "schemas" / "riverhog-collection-v1.schema.json"
    validator = Draft202012Validator(json.loads(schema_path.read_text()))
    record = PortableCollectionRecord.create(
        collection=7,
        content_identity="a" * 64,
        encryption_format="age-v1-scrypt",
        passphrase_id="collection-test-key-v1",
        provenance_mode="omitted",
        provenance_identity=None,
        metadata_revision=2,
        tags=("archive",),
        files=(("valid.txt", 1, "b" * 64),),
    ).to_mapping()

    validator.validate(record)
    record["provenance_identity"] = "c" * 64
    assert list(validator.iter_errors(record))

from __future__ import annotations

import json
from pathlib import Path

from riverhog_protocol import PortableCollectionRecord


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

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError
from riverhog_protocol import (
    ArchiveStoreName,
    RetrievalFileReferenceSetDocument,
    validate_archive_store_name,
)
from riverhog_protocol.paths import CanonicalRelPath


def test_retrieval_reference_set_is_exact_and_canonical() -> None:
    document = RetrievalFileReferenceSetDocument.model_validate(
        {
            "files": [
                {"collection_id": 1, "path": "a.txt"},
                {"collection_id": 2, "path": "nested/b.txt"},
            ]
        }
    )

    assert [(item.collection_id, item.path) for item in document.files] == [
        (1, "a.txt"),
        (2, "nested/b.txt"),
    ]


@pytest.mark.parametrize(
    "files",
    (
        [
            {"collection_id": 1, "path": "a.txt"},
            {"collection_id": 1, "path": "a.txt"},
        ],
        [
            {"collection_id": 2, "path": "b.txt"},
            {"collection_id": 1, "path": "a.txt"},
        ],
        [{"collection_id": 1, "path": " a.txt"}],
        [{"collection_id": 1, "path": "a/../b.txt"}],
    ),
)
def test_retrieval_reference_set_rejects_aliases_duplicates_and_noncanonical_order(
    files: list[dict[str, object]],
) -> None:
    with pytest.raises(ValidationError):
        RetrievalFileReferenceSetDocument.model_validate({"files": files})


def test_canonical_path_schema_advertises_semantic_exclusions() -> None:
    schema = TypeAdapter(CanonicalRelPath).json_schema()

    assert schema["format"] == "riverhog-canonical-relpath-v1"
    assert {entry["not"]["pattern"] for entry in schema["allOf"]} == {
        r"(?:^|/)\.{1,2}(?:/|$)",
        r"^\s|\s$",
    }


@pytest.mark.parametrize("value", ("archive", "aws-deep-archive", "b2"))
def test_archive_store_name_is_provider_agnostic_and_canonical(value: str) -> None:
    assert TypeAdapter(ArchiveStoreName).validate_python(value, strict=True) == value


@pytest.mark.parametrize("value", ("Archive", " archive", "archive_1", "archive--one"))
def test_archive_store_name_rejects_aliases(value: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(ArchiveStoreName).validate_python(value, strict=True)
    with pytest.raises(ValueError):
        validate_archive_store_name(value)

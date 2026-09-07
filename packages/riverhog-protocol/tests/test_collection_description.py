from __future__ import annotations

import json

import pytest
from pydantic import TypeAdapter, ValidationError
from riverhog_protocol import (
    COLLECTION_DESCRIPTION_UTF8_BYTES_MAX,
    CollectionDescription,
    CollectionDescriptionDocument,
    collection_description_identity,
)

DESCRIPTION = TypeAdapter(CollectionDescription)
ROOT = "a" * 64


def test_description_document_is_canonical_revision_sensitive_and_round_trips() -> None:
    first = CollectionDescriptionDocument.seal(
        archive_root_sha256=ROOT,
        revision=1,
        description="Résumé of 東京 footage",
    )
    later = CollectionDescriptionDocument.seal(
        archive_root_sha256=ROOT,
        revision=2,
        description=first.description,
    )

    assert first.description_identity != later.description_identity
    assert CollectionDescriptionDocument.from_json_bytes(first.to_json_bytes()) == first
    assert first.to_json_bytes() == json.dumps(
        first.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def test_description_identity_binds_root_revision_and_nullable_value() -> None:
    identity = collection_description_identity(
        archive_root_sha256=ROOT,
        revision=4,
        description=None,
    )
    assert identity != collection_description_identity(
        archive_root_sha256="b" * 64,
        revision=4,
        description=None,
    )
    assert identity != collection_description_identity(
        archive_root_sha256=ROOT,
        revision=5,
        description=None,
    )


def test_description_contract_accepts_exact_bounded_nfc_unicode() -> None:
    value = "Résumé of 東京 footage\nCaptured at dawn"
    assert DESCRIPTION.validate_python(value) == value
    assert DESCRIPTION.validate_python("a" * COLLECTION_DESCRIPTION_UTF8_BYTES_MAX) == (
        "a" * COLLECTION_DESCRIPTION_UTF8_BYTES_MAX
    )
    assert DESCRIPTION.validate_python("🦆" * (COLLECTION_DESCRIPTION_UTF8_BYTES_MAX // 4))


@pytest.mark.parametrize(
    "value",
    (
        "",
        " \t\n",
        "e\u0301",
        "contains\x00control",
        "a" * (COLLECTION_DESCRIPTION_UTF8_BYTES_MAX + 1),
        "🦆" * (COLLECTION_DESCRIPTION_UTF8_BYTES_MAX // 4 + 1),
    ),
)
def test_description_contract_rejects_noncanonical_or_oversized_text(value: str) -> None:
    with pytest.raises(ValidationError):
        DESCRIPTION.validate_python(value)


def test_description_document_rejects_noncanonical_json_and_wrong_identity() -> None:
    document = CollectionDescriptionDocument.seal(
        archive_root_sha256=ROOT,
        revision=1,
        description="Camera seven",
    )
    with pytest.raises(ValueError, match="canonical"):
        CollectionDescriptionDocument.from_json_bytes(
            json.dumps(document.model_dump(mode="json"), indent=2)
        )
    payload = document.model_dump(mode="json")
    payload["description_identity"] = "0" * 64
    with pytest.raises(ValueError, match="invalid"):
        CollectionDescriptionDocument.from_json_bytes(
            json.dumps(payload, sort_keys=True, separators=(",", ":"))
        )

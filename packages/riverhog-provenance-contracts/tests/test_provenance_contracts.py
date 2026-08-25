from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError
from riverhog_provenance_contracts import ProvenanceEntryId, ProvenanceJournalStateReference

JOURNAL_ID = "urn:uuid:00000000-0000-7000-8000-000000000001"
STATE_ID = "urn:uuid:00000000-0000-7000-8000-000000000002"
ENTRY_ID = "urn:uuid:00000000-0000-7000-8000-000000000003"


def test_provenance_reference_has_one_exact_public_identity_grammar() -> None:
    reference = ProvenanceJournalStateReference(
        journal_id=JOURNAL_ID,
        current_state_id=STATE_ID,
    )

    assert reference.model_dump() == {
        "journal_id": JOURNAL_ID,
        "current_state_id": STATE_ID,
    }
    schema = ProvenanceJournalStateReference.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["journal_id"] == {"$ref": "#/$defs/ProvenanceJournalId"}
    assert schema["$defs"]["ProvenanceJournalId"]["pattern"].startswith("^urn:uuid:")


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("journal_id", "journal-1"),
        ("journal_id", JOURNAL_ID.upper()),
        ("current_state_id", "urn:uuid:not-a-uuid"),
    ),
)
def test_provenance_reference_rejects_identity_aliases(field: str, value: str) -> None:
    payload = {"journal_id": JOURNAL_ID, "current_state_id": STATE_ID, field: value}

    with pytest.raises(ValidationError):
        ProvenanceJournalStateReference.model_validate(payload)


def test_provenance_entry_identity_uses_the_journal_schema_grammar() -> None:
    adapter = TypeAdapter(ProvenanceEntryId)

    assert adapter.validate_python(ENTRY_ID, strict=True) == ENTRY_ID
    for value in ("entry-1", ENTRY_ID.upper(), "urn:uuid:not-a-uuid"):
        with pytest.raises(ValidationError):
            adapter.validate_python(value, strict=True)

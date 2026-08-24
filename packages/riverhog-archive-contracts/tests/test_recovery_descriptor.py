from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from riverhog_archive_contracts import (
    ARCHIVE_ENCRYPTION_FORMAT,
    ArchiveRootCiphertextIdentity,
    CollectionEncryptionBinding,
    RecoveryDescriptor,
    RecoveryDescriptorError,
)

ROOT = Path(__file__).resolve().parents[1]
PASSPHRASE_ID = "c7L9vQ2mN4xR8sT1wY6zK3pH"


def _descriptor() -> RecoveryDescriptor:
    return RecoveryDescriptor(
        encryption=CollectionEncryptionBinding(
            format=ARCHIVE_ENCRYPTION_FORMAT,
            passphrase_id=PASSPHRASE_ID,
        ),
        root=ArchiveRootCiphertextIdentity(
            path="manifest.json.age",
            stored_bytes=123,
            stored_sha256="a" * 64,
        ),
    )


def test_descriptor_is_canonical_and_matches_formal_schema() -> None:
    content = _descriptor().to_json_bytes()

    assert RecoveryDescriptor.from_json_bytes(content) == _descriptor()
    schema = json.loads(
        (ROOT / "schemas" / "riverhog-recovery-descriptor-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    jsonschema.validate(json.loads(content), schema)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"schema": "riverhog-recovery-descriptor/v1", "encryption": {}, "root": {}},
        {
            "schema": "riverhog-recovery-descriptor/v1",
            "encryption": {"format": "future", "passphrase_id": PASSPHRASE_ID},
            "root": {
                "path": "manifest.json.age",
                "stored_bytes": 123,
                "stored_sha256": "a" * 64,
            },
        },
    ],
)
def test_descriptor_rejects_values_outside_the_v1_contract(payload: object) -> None:
    with pytest.raises(RecoveryDescriptorError):
        RecoveryDescriptor.from_json_bytes(
            json.dumps(payload, sort_keys=True, separators=(",", ":"))
        )


def test_descriptor_rejects_noncanonical_json() -> None:
    content = json.dumps(json.loads(_descriptor().to_json_bytes()), indent=2)

    with pytest.raises(RecoveryDescriptorError, match="canonical"):
        RecoveryDescriptor.from_json_bytes(content)
